"""Own one finite child phase, preserve exit/deadline evidence, never retry."""
import argparse
import os
import signal
import subprocess
import sys
from common import BASE, ROOT, save, stamp


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('inputs','baseline','candidate','asr-baseline','asr-candidate'))
    args=parser.parse_args()
    phase=args.phase
    if phase=='inputs':
        command=[sys.executable,str(BASE/'make_inputs.py')]; deadline=360
    elif phase.startswith('asr-'):
        arm=ROOT/'results'/('iteration19-'+phase[4:])
        command=[sys.executable,str(BASE/'review_received_audio.py'),'--suite-root',str(arm),'--out',str(arm/'received-audio-review')];deadline=600
    else:
        command=[sys.executable,str(BASE/'run_suite.py'),'--arm',phase];deadline=1500
    path=BASE/f'{phase}-supervision.json'
    report=dict(status='starting',started_at_utc=stamp(),command=command,deadline_seconds=deadline,sigterm_grace_seconds=150 if phase in {'baseline','candidate'} else 20)
    save(path,report,exclusive=True)
    def interrupt(signum,frame):
        raise KeyboardInterrupt(f'signal {signum}')
    signal.signal(signal.SIGTERM,interrupt);signal.signal(signal.SIGINT,interrupt)
    process=None
    try:
        with (BASE/f'{phase}-controller.log').open('x') as log:
            process=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            report.update(status='running',pid=process.pid);save(path,report)
            try:
                process.wait(timeout=deadline)
            except subprocess.TimeoutExpired:
                report['deadline_exceeded']=True
            finally:
                if process.poll() is None:
                    report['sigterm_sent']=True;save(path,report);os.killpg(process.pid,signal.SIGTERM)
                    try:
                        process.wait(timeout=report['sigterm_grace_seconds'])
                    except subprocess.TimeoutExpired:
                        report['forced_kill']=True;save(path,report);os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=10)
                report.update(status='exited',exit_code=process.returncode)
    except BaseException as error:
        report['supervisor_error']=repr(error)
        raise
    finally:
        report['finished_at_utc']=stamp();save(path,report)
    print(report,flush=True)
    return int(report.get('exit_code')!=0 or bool(report.get('deadline_exceeded')))

if __name__=='__main__':
    raise SystemExit(main())
