import asyncio
import audioop
from .AudioFilter import AudioFilter
import functools
import threading
import traceback

import av
from dico.voice import AudioBase

from .AudioFifo import AudioFifo
from .utils.threadLock import withLock

AVOption = {
    "err_detect": "ignore_err",
    "reconnect": "1",
    "reconnect_streamed": "1",
    "reconnect_delay_max": "5",
}


class PyAVSource(AudioBase):
    def __init__(self, Source: str, AVOption: dict = AVOption) -> None:
        self.loop = asyncio.get_event_loop()

        self.Source = Source
        self.AVOption = AVOption
        self.Container = None  # av.StreamContainer
        self.selectAudioStream = self.FrameGenerator = None

        self._end = threading.Event()
        self._haveToReloadResampler = threading.Event()
        self._waitforread = threading.Lock()
        self._loading = threading.Lock()
        self._seeking = threading.Lock()
        self.BufferLoader = None

        self.AudioFifo = AudioFifo()
        self.duration = None
        self._position = 0.0
        self._volume = 1.0
        self.filter = {}

    def __del__(self):
        self.cleanup()

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._volume = max(value, 0.0)

    @property
    def position(self) -> float:
        return round(
            self._position - self.AudioFifo.samples / 960 / 50,
            2,
        )

    def read(self) -> bytes:
        if not self.BufferLoader:
            self.start()

        if not self.AudioFifo:
            return

        Data = self.AudioFifo.read()
        if not Data and self._loading.locked():
            while self._loading.locked():
                if not self._waitforread.locked():
                    self._waitforread.acquire()
                self._waitforread.acquire()

                Data = self.AudioFifo.read()
                if Data:
                    break

        if Data and self.volume != 1.0:
            Data = audioop.mul(Data, 2, min(self._volume, 2.0))

        return Data

    def _seek(self, offset: float, *args, **kwargs) -> None:
        with withLock(self._seeking):
            if not self.Container:
                if not self._loading.locked():
                    self.Container = av.open(
                        self.Source.Source, options=self.Source.AVOption
                    )
                else:
                    while not self.Container:
                        pass

            kwargs["any_frame"] = True

            self.Container.seek(round(max(offset, 1) * 1000000), *args, **kwargs)
            self.reload()

    async def seek(self, offset: float, *args, **kwargs) -> None:
        return await self.loop.run_in_executor(
            None, functools.partial(self._seek, offset, *args, **kwargs)
        )

    def reload(self) -> None:
        self._haveToReloadResampler.set()

        if not self._loading.locked():
            if self._end.is_set():
                self._end.clear()
            self.start()

    def start(self) -> None:
        self.BufferLoader = Loader(self)
        self.BufferLoader.start()

    def stop(self) -> None:
        self._end.set()

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.stop()
        if self.AudioFifo and not self.AudioFifo.haveToFillBuffer.is_set():
            self.AudioFifo.haveToFillBuffer.set()
        self.AudioFifo = None


class Loader(threading.Thread):
    def __init__(self, AudioSource: PyAVSource) -> None:
        threading.Thread.__init__(self)
        self.daemon = True

        self.Source = AudioSource

        self.Resampler = None
        self.Filter = {}
        self.FilterGraph = None

    def _do_run(self) -> None:
        with withLock(self.Source._loading):
            if not self.Source.Container:
                self.Source.Container = av.open(
                    self.Source.Source, options=self.Source.AVOption
                )
            self.Source.duration = round(self.Source.Container.duration / 1000000, 2)

            self.Source.selectAudioStream = self.Source.Container.streams.audio[0]
            self.Source.FrameGenerator = self.Source.Container.decode(
                self.Source.selectAudioStream
            )

            while not self.Source._end.is_set():
                if self.Source.filter != self.Filter:
                    self.Filter = self.Source.filter

                    if self.Source.filter:
                        self.FilterGraph = AudioFilter()
                        self.FilterGraph.selectAudioStream = (
                            self.Source.selectAudioStream
                        )
                        self.FilterGraph.setFilters(self.Filter)
                    else:
                        self.FilterGraph = None

                if not self.Resampler or self.Source._haveToReloadResampler.is_set():
                    self.Resampler = av.AudioResampler(
                        format=av.AudioFormat("s16").packed, layout="stereo", rate=48000
                    )
                    self.Source._haveToReloadResampler.clear()

                _seek_locked = False
                if self.Source._seeking.locked():
                    self.Source._seeking.acquire()
                    _seek_locked = True

                Frame = next(self.Source.FrameGenerator, None)

                if _seek_locked:
                    self.Source._seeking.release()
                    self.Source.AudioFifo.reset()

                if not Frame:
                    self.Source.stop()
                    break

                _current_position = float(Frame.pts * Frame.time_base)

                if self.FilterGraph:
                    self.FilterGraph.push(Frame)
                    Frame = self.FilterGraph.pull()

                    if not Frame:
                        continue

                Frame.pts = None
                try:
                    Frame = self.Resampler.resample(Frame)
                except ValueError:
                    self.Source._haveToReloadResampler.set()
                    continue

                if not self.Source.AudioFifo.haveToFillBuffer.is_set():
                    self.Source.AudioFifo.haveToFillBuffer.wait()

                self.Source.AudioFifo.write(Frame)
                self.Source._position = _current_position

                if self.Source._waitforread.locked():
                    self.Source._waitforread.release()

    def run(self) -> None:
        try:
            self._do_run()
        except:
            traceback.print_exc()
        finally:
            if self.Source.Container:
                self.Source.Container.close()
                self.Source.Container = None

            self.Source.stop()
