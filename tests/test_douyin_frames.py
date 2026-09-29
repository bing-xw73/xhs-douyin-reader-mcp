import pathlib
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from douyin_frames import DouyinFrames, options, probe_file, transcribe_audio
from xhs_reader import ReaderError

URL='https://v.douyin.com/ipohcwlD9Xs/'

class FrameTests(unittest.TestCase):
    def setUp(self):
        self.reader=Mock()
        self.reader.metadata.return_value={'duration_seconds':61.1,'size_bytes':100,'_play_urls':['https://v.zjcdn.com/video']}
        self.worker=DouyinFrames(self.reader)

    def test_options(self):
        options(4,False)
        for count in (0,9,True,'4',1.5):
            with self.assertRaises(ReaderError): options(count,False)
        with self.assertRaises(ReaderError): options(4,'true')

    def test_duration_and_size_preflight(self):
        for data in ({'duration_seconds':301}, {'duration_seconds':float('nan')},
                     {'duration_seconds':61,'size_bytes':100_000_001}):
            with self.assertRaises(ReaderError):
                self.worker.download(data,pathlib.Path('unused'),time.monotonic()+10)
        self.reader.open_media.assert_not_called()

    def test_actual_duration_and_resolution_checked(self):
        for data in (b'{"format":{"duration":"301"},"streams":[{"codec_type":"video","width":1280,"height":720}]}',
                     b'{"format":{"duration":"61"},"streams":[{"codec_type":"video","width":3840,"height":2160}]}'):
            with patch('douyin_frames.run_media',return_value=data), self.assertRaises(ReaderError):
                probe_file(pathlib.Path('unused'),time.monotonic()+10)

    def test_download_streaming_size_limit(self):
        conn,res=Mock(),Mock()
        res.status=200
        res.getheader.return_value=None
        res.read1.side_effect=[b'\x00\x00\x00\x20ftypisom123456789']
        self.reader.open_media.return_value=(conn,res)
        with tempfile.TemporaryDirectory() as tmp, patch('douyin_frames.MAX_VIDEO_BYTES',16):
            with self.assertRaises(ReaderError) as error:
                self.worker.download({'duration_seconds':1,'_play_urls':['https://v.zjcdn.com/v']},pathlib.Path(tmp)/'v.mp4',time.monotonic()+10)
            self.assertEqual(error.exception.code,'VIDEO_SIZE_LIMIT')
        conn.close.assert_called_once()

    def test_cleanup_success_and_cached_no_redownload(self):
        locations=[]
        def process(metadata,folder,count,transcribe,deadline):
            locations.append(folder)
            (folder/'video.mp4').write_bytes(b'temporary')
            return {'ok':True,'partial':False,'cached':False},[{'jpeg':b'jpeg'}]
        with patch.object(self.worker,'process',side_effect=process) as proc:
            result,_=self.worker.read(URL)
            self.assertTrue(result['temporary_files_deleted'])
            result,_=self.worker.read(URL)
            self.assertTrue(result['cached'])
            self.assertEqual(proc.call_count,1)
        self.assertTrue(all(not p.exists() for p in locations))

    def test_cleanup_failure_redacts_exception(self):
        locations=[]
        def process(metadata,folder,*args):
            locations.append(folder)
            (folder/'audio.wav').write_bytes(b'temporary')
            raise RuntimeError('sensitive-value-must-not-appear')
        with patch.object(self.worker,'process',side_effect=process):
            result,parts=self.worker.read(URL)
        self.assertFalse(result['ok'])
        self.assertNotIn('sensitive-value',str(result))
        self.assertEqual(parts,[])
        self.assertTrue(all(not p.exists() for p in locations))

    def test_single_processing_job(self):
        with self.worker.lock:
            result,_=self.worker.read(URL)
        self.assertEqual(result['error']['code'],'VIDEO_PROCESSOR_BUSY')

    def test_expired_play_url_refresh_once(self):
        with patch.object(self.worker,'process',side_effect=[ReaderError('VIDEO_DOWNLOAD_FAILED','expired'),
               ({'ok':True,'partial':False},[{'jpeg':b'x'}])]):
            result,_=self.worker.read(URL)
        self.assertTrue(result['ok'])
        self.reader.metadata.assert_called_with(URL,force=True)

    def test_asr_error_does_not_echo_credentials(self):
        with patch.dict('os.environ',{'SILICONFLOW_API_KEY':'secret'}), tempfile.TemporaryDirectory() as tmp:
            audio=pathlib.Path(tmp)/'a.wav'; audio.write_bytes(b'audio')
            with patch('douyin_frames.httpx.Client',side_effect=RuntimeError('secret')):
                with self.assertRaises(ReaderError) as error: transcribe_audio(audio,time.monotonic()+10)
            self.assertNotIn('secret',str(error.exception))

if __name__=='__main__': unittest.main()
