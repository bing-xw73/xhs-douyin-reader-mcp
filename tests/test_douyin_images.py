import copy
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch
from PIL import Image
from douyin_reader import DouyinReader, image_sources, page_url, parse_item, post_url
from douyin_images import DouyinImages, download_image
from douyin_frames import DouyinFrames
from xhs_reader import ReaderError

ID='7159749791113645325'
NOTE='https://www.douyin.com/note/'+ID
SLIDES='https://www.iesdouyin.com/share/slides/'+ID+'/'
CDN='https://p11-sign.douyinpic.com/'
ITEM={'aweme_id':ID,'aweme_type':2,'desc':'图文示例','author':{'nickname':'作者'},
      'music':{'title':'background'},'video':{'duration':10000,'play_addr':{'url_list':['https://aweme.snssdk.com/music']}},
      'images':[{'url_list':[CDN+str(i)+'.webp']} for i in range(5)]}

def image_bytes(size=(400,300)):
    out=io.BytesIO()
    with Image.new('RGB',size,'white') as image: image.save(out,format='PNG')
    return out.getvalue()

class GalleryTests(unittest.TestCase):
    def reader(self):
        reader=DouyinReader()
        reader.item=Mock(return_value=copy.deepcopy(ITEM))
        reader.probe=Mock(side_effect=AssertionError('image post must never probe music/video'))
        return reader

    def worker(self,size=(400,300)):
        worker=DouyinImages(self.reader())
        worker.download=Mock(return_value=image_bytes(size))
        return worker

    def test_note_slides_routes(self):
        for url in (NOTE,SLIDES,'https://m.douyin.com/slides/'+ID,'https://www.douyin.com/share/note/'+ID):
            self.assertEqual(page_url(post_url(url)),'https://m.douyin.com/share/note/'+ID)
        with self.assertRaises(ReaderError): post_url('https://douyin.com.evil.com/note/'+ID)

    def test_shortlink_redirect_to_gallery(self):
        reader=DouyinReader()
        first,second=Mock(),Mock()
        first.status=302; first.getheaders.return_value=[]
        first.getheader.side_effect=lambda k:SLIDES if k=='Location' else None
        second.status=200; second.getheaders.return_value=[]; second.getheader.return_value=None
        second.read.return_value=b'page'
        conns=[Mock(),Mock()]
        with patch.object(reader,'connection',side_effect=[(conns[0],first),(conns[1],second)]) as get:
            reader.page('https://v.douyin.com/abcdef/',None)
            self.assertEqual(get.call_args_list[1].args[0],'https://m.douyin.com/share/note/'+ID)
        self.assertTrue(all(c.close.called for c in conns))

    def test_parse_each_loader_and_id_validation(self):
        for route in ('note','slides','video'):
            page='window._ROUTER_DATA = '+json.dumps({'loaderData':{route+'_(id)/page':{'videoInfoRes':{'item_list':[ITEM]}}}})
            self.assertEqual(parse_item(page,NOTE)['images'],ITEM['images'])
            with self.assertRaises(ReaderError): parse_item(page,NOTE.replace(ID,'1111111111111111111'))

    def test_images_prioritized_over_video_and_music(self):
        reader=self.reader()
        result=reader.read(NOTE)
        self.assertTrue(result['ok'])
        self.assertEqual(result['type'],'images')
        self.assertEqual(result['image_count'],5)
        self.assertEqual(result['body'],'图文示例')
        self.assertNotIn('_image_sources',result)
        self.assertEqual(result['transcription_status'],'not_applicable_image_post')
        self.assertTrue(reader.read(SLIDES)['cached'])
        self.assertEqual(reader.item.call_count,1)
        reader.probe.assert_not_called()

    def test_background_audio_never_processed(self):
        reader=self.reader(); frames=DouyinFrames(reader)
        with patch.object(frames,'process') as process, patch('douyin_frames.transcribe_audio') as asr:
            result,parts=frames.read(NOTE,4,True)
            self.assertEqual(result['error']['code'],'IMAGE_POST_USE_IMAGES')
            self.assertEqual(parts,[])
            process.assert_not_called(); asr.assert_not_called()

    def test_default_and_selected_order_cache(self):
        worker=self.worker()
        result,parts=worker.read(NOTE)
        self.assertTrue(result['ok']); self.assertEqual(result['returned_indexes'],[1,2])
        self.assertEqual([p['index'] for p in parts],[1,2])
        result,parts=worker.read(SLIDES,[2,1])
        self.assertEqual(result['returned_indexes'],[2,1])
        self.assertTrue(all(s['cached'] for s in result['images']))
        self.assertEqual(worker.download.call_count,2)

    def test_four_and_invalid_indexes(self):
        worker=self.worker()
        result,_=worker.read(NOTE,[1,2,3,4])
        self.assertEqual(result['returned_indexes'],[1,2,3,4])
        for indexes in ([1,2,3,4,5],[],[True],[1,1]):
            with self.subTest(indexes=indexes):
                result,_=self.worker().read(NOTE,indexes)
                self.assertEqual(result['error']['code'],'INVALID_INDEXES')
        result,_=self.worker().read(NOTE,[6])
        self.assertEqual(result['error']['code'],'INDEX_OUT_OF_RANGE')

    def test_long_picture_tiling_and_jpeg(self):
        result,parts=self.worker((500,2100)).read(NOTE,[1])
        self.assertTrue(result['ok']); self.assertGreater(len(parts),1)
        for part in parts:
            with Image.open(io.BytesIO(part['jpeg'])) as im:
                self.assertEqual(im.format,'JPEG'); self.assertLessEqual(max(im.size),1568)

    def test_invalid_source_preserves_indexes(self):
        item=copy.deepcopy(ITEM)
        item['images'][0]['url_list']=['https://evil.com/photo.jpg']
        self.assertEqual(image_sources(item)[0],[])
        worker=self.worker(); worker.reader.item.return_value=item
        result,_=worker.read(NOTE)
        self.assertTrue(result['partial']); self.assertEqual(result['returned_indexes'],[2])
        self.assertEqual(result['errors'][0]['index'],1)

    def test_video_post_redirects_to_frames(self):
        worker=self.worker()
        worker.reader.metadata=Mock(return_value={'type':'video'})
        result,_=worker.read(NOTE)
        self.assertEqual(result['error']['code'],'VIDEO_POST_USE_FRAMES')
        worker.download.assert_not_called()

    def test_shared_job_lock(self):
        lock=threading.Lock(); worker=DouyinImages(self.reader(),lock)
        with lock:
            result,_=worker.read(NOTE)
        self.assertEqual(result['error']['code'],'VIDEO_PROCESSOR_BUSY')

    def test_shared_call_limit(self):
        worker=self.worker()
        for _ in range(5): self.assertTrue(worker.read(NOTE,[1])[0]['ok'])
        self.assertEqual(worker.reader.read(NOTE)['error']['code'],'RATE_LIMITED')

    def test_cdn_redirect_validation_no_cookie(self):
        reader=Mock(); conn,res=Mock(),Mock()
        reader.connection.return_value=(conn,res); res.status=302
        private_url='http://' + '.'.join(('127','0','0','1')) + '/private'
        res.getheader.side_effect=lambda k:private_url if k=='Location' else None
        with self.assertRaises(ReaderError): download_image(CDN+'a.jpg',reader)
        conn.close.assert_called_once()
        self.assertNotIn('Cookie',reader.connection.call_args.args[2])

    def test_download_rejects_non_image_and_oversize(self):
        for content_type,size,code in [('text/html','100','INVALID_IMAGE_TYPE'),('image/jpeg',str(11*1024*1024),'IMAGE_TOO_LARGE')]:
            reader=Mock(); conn,res=Mock(),Mock(); res.status=200
            res.getheader.side_effect=lambda k:{'Content-Type':content_type,'Content-Length':size}.get(k)
            reader.connection.return_value=(conn,res)
            with self.assertRaises(ReaderError) as error: download_image(CDN+'a.jpg',reader)
            self.assertEqual(error.exception.code,code)
            conn.close.assert_called_once()

if __name__=='__main__': unittest.main()
