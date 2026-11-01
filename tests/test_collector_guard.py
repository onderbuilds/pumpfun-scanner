import sys, os, unittest, subprocess, tempfile, threading
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
class TestCollectorGuard(unittest.TestCase):
    def test_import_does_not_start(self):
        r=subprocess.run([sys.executable,'-c',"import threading,collector_v2 as c; import asyncio; print('threads',threading.active_count())"],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(r.returncode,0,r.stderr); self.assertIn('threads 1',r.stdout)
    def test_second_instance_refuses(self):
        lock=os.path.join(tempfile.mkdtemp(),'c.lock')
        code=f"import collector_v2 as c,time; c.log=lambda *a: print(*a); L=c._single_instance_lock({lock!r}); print('GOT',flush=True); time.sleep(6)"
        a=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,stdout=subprocess.PIPE,text=True)
        self.assertEqual(a.stdout.readline().strip(),'GOT')
        b=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(b.returncode,3); self.assertIn('REFUSING',b.stdout); a.kill(); a.wait()
if __name__=='__main__': unittest.main(verbosity=2)
