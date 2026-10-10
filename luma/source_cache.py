"""Bounded, thread-safe decoded images; no persisted or rendered edits."""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
from threading import RLock
import json
import hashlib


class DecodedSourceCache:
    def __init__(self,max_bytes=512*1024*1024,max_entries=12):
        self.max_bytes=max(0,int(max_bytes));self.max_entries=max(0,int(max_entries))
        self.entries=OrderedDict();self.inflight={};self.generation=0;self.lock=RLock()
        self.bytes=0;self.hits=0;self.misses=0;self.shared=0

    def clear(self):
        with self.lock:
            self.generation+=1;self.entries.clear();self.bytes=0

    @staticmethod
    def fingerprint(path):
        path=Path(path).resolve();stat=path.stat()
        # Windows can assign identical timestamps to rapid same-size rewrites.
        # Read all small files, or bounded head/middle/tail samples for large RAWs.
        # This keeps cache hits independent of full-file decoding/hash cost.
        block=65536;digest=hashlib.blake2b(digest_size=16)
        with path.open('rb') as stream:
            if stat.st_size<=block*3:digest.update(stream.read())
            else:
                for offset in (0,(stat.st_size-block)//2,stat.st_size-block):
                    stream.seek(offset);digest.update(stream.read(block))
        return (str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino,digest.digest())

    @staticmethod
    def decode_options(settings):
        from .rawcolor import enabled
        # RAW WB/DCP/tone edits develop the same sensor buffer. Legacy and
        # camera-native decoding, working RGB and video frame/color do differ.
        return (settings.get('working_space','sRGB'),enabled(settings),settings.get('video_time',0),
                json.dumps(settings.get('video_color'),sort_keys=True,separators=(',',':'),allow_nan=False))

    def load(self,path,settings,max_size,decode,*,offline=None):
        """Stat before/after decoding; retries once if an external writer changed it.

        ``decode`` returns (array, metadata). Its array becomes read-only; callers
        receive independent metadata. Missing originals use a separately keyed
        offline preview, so a reconnect cannot return the offline cached buffer.
        """
        path=Path(path);options=self.decode_options(settings)
        for attempt in range(2):
            origin=path if path.is_file() else Path(offline) if offline is not None else path
            before=self.fingerprint(origin)
            with self.lock:
                generation=self.generation;key=(generation,before,options,max_size,origin!=path)
                previous=self.entries.pop(key,None)
                if previous is not None:
                    self.entries[key]=previous;self.hits+=1
                    return previous[0],deepcopy(previous[1])
                future=self.inflight.get(key)
                if future is None:
                    future=Future();self.inflight[key]=future;owner=True;self.misses+=1
                else:owner=False;self.shared+=1
            if not owner:
                result=future.result()
                if result is None:continue
                return result[0],deepcopy(result[1])
            try:
                result=decode();after=self.fingerprint(origin)
                # A reconnect/disconnect during decode also changes the source.
                current=path if path.is_file() else Path(offline) if offline is not None else path
                if before!=after or current!=origin:
                    future.set_result(None);continue
                pixels,metadata=result;pixels.setflags(write=False);stored=(pixels,deepcopy(metadata))
                with self.lock:
                    if generation==self.generation and self.max_bytes and pixels.nbytes<=self.max_bytes and self.max_entries:
                        while self.entries and (self.bytes+pixels.nbytes>self.max_bytes or len(self.entries)>=self.max_entries):
                            _,old=self.entries.popitem(last=False);self.bytes-=old[0].nbytes
                        self.entries[key]=stored;self.bytes+=pixels.nbytes
                future.set_result(stored)
                return pixels,deepcopy(metadata)
            except BaseException as error:
                future.set_exception(error);raise
            finally:
                with self.lock:self.inflight.pop(key,None)
        raise OSError('읽는 동안 원본이 변경되었습니다. 사진을 다시 선택해 주세요.')
