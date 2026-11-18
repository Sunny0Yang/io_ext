#!/usr/bin/env python3
"""Independent, fail-visible M2 completion-latency histogram collection in virtme.
Not a replacement for the main vm framework. All results are immutable per run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import traceback

ARMS = [
    ('a_none', 'none'), ('b_mqdl_prio', 'mq-deadline'),
    ('c_bfq', 'bfq'), ('d_bfq_weight', 'bfq'), ('e_io_max', 'none'),
    ('g_io_cost', 'none'), ('h_io_cost_weight', 'none'),
    ('i_native_kyber', 'kyber'), ('f_io_latency', 'none'),
]
ROOT = Path('/sys/fs/cgroup')
SYS = Path('/sys/block/nvme0n1')
DEVICE = '/dev/nvme0n1'


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def write(path, value):
    path.write_text(str(value) + '\n')


def snapshot(cgroups):
    result = {'epoch': time.time(), 'monotonic': time.monotonic(), 'scheduler': (SYS/'queue/scheduler').read_text(), 'groups': {}}
    for cg in [ROOT] + cgroups:
        item = {}
        for name in ['io.stat', 'io.max', 'io.weight', 'io.bfq.weight', 'io.latency', 'io.prio.class', 'io.cost.model', 'io.cost.qos', 'cgroup.procs', 'cgroup.events', 'cgroup.subtree_control']:
            p = cg/name
            if p.exists():
                item[name] = p.read_text()
        members = {}
        for pid in item.get('cgroup.procs', '').split():
            try:
                proc = Path('/proc')/pid
                members[pid] = {'cgroup': (proc/'cgroup').read_text(), 'cmdline': (proc/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')}
            except FileNotFoundError:
                pass
        item['actual_processes'] = members
        result['groups'][str(cg)] = item
    return result


def fio_command(out, name, runtime, delay, rate=None):
    cmd = ['/usr/bin/fio', '--name='+name, '--filename='+DEVICE, '--readonly', '--allow_file_create=0', '--ioengine=io_uring', '--direct=1', '--bs=64k', '--rw=randread', '--iodepth=8', '--numjobs=1', '--runtime='+str(runtime), '--time_based=1', '--startdelay='+str(delay), '--randrepeat=1', '--randseed=20261103', '--group_reporting=1', '--output-format=json+', '--output='+str(out/'fio.json'), '--write_hist_log='+str(out/'hist'), '--log_hist_msec=1000', '--log_hist_coarseness=0', '--log_unix_epoch=1', '--write_bw_log='+str(out/'bw'), '--log_avg_msec=1000', '--clat_percentiles=1', '--lat_percentiles=0']
    if rate is not None:
        cmd.append('--rate='+str(rate)+'M')
    return cmd


def launch(cmd, cg, out):
    save(out/'command.json', cmd)
    # The shell moves itself before exec; all fio workers inherit membership.
    wrapper = ['bash', '-c', 'printf "%s\\n" "$$" > "$1/cgroup.procs"; shift; exec "$@"', 'm2p99', str(cg)] + cmd
    stdout = open(out/'stdout.log', 'wb')
    stderr = open(out/'stderr.log', 'wb')
    proc = subprocess.Popen(wrapper, stdout=stdout, stderr=stderr, start_new_session=True)
    stdout.close(); stderr.close()
    return proc


def run_arm(output, key, scheduler, calib, probe=False):
    arm_dir = output/('probe' if probe else 'M2P99_'+key)
    arm_dir.mkdir(exist_ok=False)
    dev = (SYS/'dev').read_text().strip()
    # Strict reset: failures propagate instead of silently contaminating later arms.
    write(ROOT/'io.cost.qos', dev+' enable=0')
    write(SYS/'queue/scheduler', scheduler)
    assert '['+scheduler+']' in (SYS/'queue/scheduler').read_text()
    if scheduler == 'bfq':
        write(SYS/'queue/iosched/low_latency', 0)
        assert (SYS/'queue/iosched/low_latency').read_text().strip() == '0'
    parent = ROOT/('m2p99_'+str(os.getpid())+'_'+key)
    parent.mkdir()
    write(parent/'cgroup.subtree_control', '+io')
    groups = [parent/t for t in 'ABC']
    processes = []
    try:
        for i, cg in enumerate(groups):
            cg.mkdir()
            if key == 'b_mqdl_prio':
                write(cg/'io.prio.class', ['promote-to-rt', 'idle', 'restrict-to-be'][i])
            elif key == 'd_bfq_weight':
                write(cg/'io.bfq.weight', str([1000,1,100][i]))
            elif key == 'e_io_max':
                limits = calib['io_max_mib_s']
                write(cg/'io.max', dev+' rbps='+str(limits[i]*1024*1024)+' wbps=max riops=max wiops=max')
            elif key == 'f_io_latency':
                write(cg/'io.latency', dev+' target='+str([20,1000,100][i]))
            elif key == 'h_io_cost_weight':
                write(cg/'io.weight', str([1000,1,100][i]))
        if key in ('g_io_cost', 'h_io_cost_weight'):
            write(ROOT/'io.cost.model', dev+' ctrl=user model=linear '+' '.join(k+'='+str(v) for k,v in calib['iocost_model'].items()))
            write(ROOT/'io.cost.qos', dev+' enable=1 ctrl=user rpct=95 rlat=20000 wpct=95 wlat=20000 min=50 max=150')
        before = snapshot([parent]+groups)
        if scheduler == 'bfq':
            before['bfq_tunables'] = {p.name:p.read_text() for p in (SYS/'queue/iosched').iterdir() if p.is_file()}
        save(arm_dir/'cgroup_before.json', before)
        t0 = time.time(); mono = time.monotonic()
        launch_times = {}
        for i, (tenant,cg) in enumerate(zip('ABC', groups)):
            sub = arm_dir/tenant; sub.mkdir()
            delay = (0 if probe else [0,10,20][i])
            runtime = (3 if probe else [50,60,30][i])
            cmd = fio_command(sub, tenant, runtime, delay, calib['rate_mib_s'])
            launch_times[tenant] = {'epoch':time.time(), 'delay':delay, 'runtime':runtime, 'cgroup':str(cg)}
            processes.append(launch(cmd,cg,sub))
            if probe:
                break
        save(arm_dir/'timing.json', {'origin_epoch':t0, 'origin_monotonic':mono, 'launches':launch_times})
        timeout = 15 if probe else 110
        with open(arm_dir/'cgroup_samples.jsonl','w') as log:
            while any(p.poll() is None for p in processes):
                log.write(json.dumps(snapshot([parent]+groups))+'\n'); log.flush()
                if time.monotonic()-mono > timeout:
                    raise TimeoutError('fio arm exceeded '+str(timeout)+' seconds')
                time.sleep(1)
        codes = [p.wait() for p in processes]
        save(arm_dir/'cgroup_after.json', snapshot([parent]+groups))
        assert all(c == 0 for c in codes), 'fio exit codes '+str(codes)
        for tenant in launch_times:
            d = json.loads((arm_dir/tenant/'fio.json').read_text())
            assert len(d['jobs']) == 1 and d['jobs'][0]['error'] == 0
            assert list((arm_dir/tenant).glob('*clat_hist*.log')), 'missing histogram'
        save(arm_dir/'status.json', {'success':True, 'exit_codes':codes, 'elapsed_s':time.monotonic()-mono})
    except BaseException:
        save(arm_dir/'status.json', {'success':False, 'traceback':traceback.format_exc()})
        raise
    finally:
        for p in processes:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
        for p in processes:
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL); p.wait()
        # Save residual I/O attribution even on failure, before removing cgroups.
        save(arm_dir/'cgroup_cleanup.json',snapshot([parent]+groups))
        for cg in groups:
            if cg.exists(): cg.rmdir()
        parent.rmdir()
        write(ROOT/'io.cost.qos',dev+' enable=0')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--phase',choices=['probe','campaign'],required=True)
    parser.add_argument('--coef-source',type=Path,default=Path('/opt/ioext/results/m2_fig2_p99_virtme/20261103T133649Z_campaign/recovered_coef_gen_source.log'),help='Archived successful coef_gen stdout; preserve its full provenance')
    args = parser.parse_args(); out=args.output
    assert os.geteuid()==0, 'guest root required'
    assert (SYS/'device/serial').read_text().strip() == 'm2p99_readonly', 'refuse unexpected device'
    # QEMU NVMe does not necessarily propagate backend readonly to BLKROGET.
    # Enforce kernel-level readonly too, after verifying our unique NVMe serial.
    subprocess.run(['blockdev','--setro',DEVICE],check=True)
    assert subprocess.check_output(['blockdev','--getro',DEVICE],text=True).strip() == '1'
    if not (ROOT/'cgroup.controllers').exists():
        subprocess.run(['mount','-t','cgroup2','none',str(ROOT)],check=True)
    write(ROOT/'cgroup.subtree_control','+io')
    save(out/'guest_environment.json',{'uname':list(os.uname()),'fio_version':subprocess.check_output(['/usr/bin/fio','--version'],text=True),'device_serial':(SYS/'device/serial').read_text(),'device_size_sectors':(SYS/'size').read_text(),'device_major_minor':(SYS/'dev').read_text(),'scheduler_available':(SYS/'queue/scheduler').read_text(),'device_readonly':True})
    # Recover the unmodified coef_gen model from the historical refresh log,
    # never from the later workload-matched replacement calibration.json.
    source = args.coef_source
    text = source.read_text()
    match = re.search(r'model refreshed: (\{[^\n]+\})',text)
    assert match, 'no recoverable coef_gen provenance'
    import ast
    model = ast.literal_eval(match.group(1))
    model = {k:int(model[k]) for k in ('rbps','rseqiops','rrandiops','wbps','wseqiops','wrandiops')}
    assert all(v>0 for v in model.values())
    (out/'recovered_coef_gen_source.log').write_text(text)
    calib={'iocost_model':model,'iocost_model_source':str(source),'iocost_model_source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'iocost_provenance_limitation':'Recovered reported successful coef_gen refresh from older log on same shared QEMU image; original per-probe JSON is unavailable. No coefficient tuning or replacement.','iocost_qos':{'rpct':95,'rlat':20000,'wpct':95,'wlat':20000,'min':50,'max':150},'iocost_qos_limitation':'Inherited explicit configuration; not claimed to be paper settings; fixed for both g and h.'}
    # Fresh solo probe only sets one uniform rate, before observing any arm.
    probe_dir=out/'solo_calibration'; probe_dir.mkdir()
    cmd=fio_command(probe_dir,'solo',15,0)
    save(probe_dir/'command.json',cmd)
    with open(probe_dir/'stdout.log','w') as stdout, open(probe_dir/'stderr.log','w') as stderr:
        subprocess.run(cmd,stdout=stdout,stderr=stderr,check=True,timeout=30)
    solo=json.loads((probe_dir/'fio.json').read_text())['jobs'][0]['read']['bw_bytes']/1024**2
    rate=min(1536,round(0.8*solo))
    assert rate>0
    calib.update(solo_mib_s=solo,rate_mib_s=rate,rate_rule='min(1536, round(0.8 * fresh solo 64KiB/QD8 MiB/s))',io_max_mib_s=[round(1500/1536*rate),round(500/1536*rate),round(500/1536*rate)],replication_limitation='QEMU file-backed readonly NVMe, not physical SSD passthrough; units and target constants inherited from old configuration, not independently authenticated paper claims.')
    save(out/'calibration.json',calib)
    completed=[]; failed=[]
    chosen=[('a_none','none')] if args.phase=='probe' else ARMS
    for key,scheduler in chosen:
        print('M2P99 BEGIN '+key,flush=True)
        try:
            run_arm(out,key,scheduler,calib,probe=args.phase=='probe')
            completed.append(key)
            print('M2P99 PASS '+key,flush=True)
        except Exception:
            failed.append({'arm':key,'traceback':traceback.format_exc()})
            traceback.print_exc()
        save(out/'guest_status.json',{'completed':completed,'failed':failed,'planned_order':chosen,'repeats':1,'selection':'all arms, one predeclared run; no aesthetic selection or retries'})
    if failed: raise SystemExit(1)


if __name__=='__main__':
    main()
