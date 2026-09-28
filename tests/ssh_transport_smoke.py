"""Real SSH transport against an isolated localhost sshd and synthetic stores only.

Run manually where OpenSSH server/keygen and local socket access are available.
No real provider session, SSH config, trusted-host file or key is used.
"""
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.manager import Manager
from backend.remote import RemoteLocations
from backend.teleport import Teleport


def main():
    with tempfile.TemporaryDirectory(prefix='gpt-ssh-smoke-') as temp:
        root = Path(temp); host = root/'remote'; local = root/'local'; project = host/'project'; binary = root/'bin'
        project.mkdir(parents=True); local.mkdir(); binary.mkdir()
        (project/'hello.txt').write_text('fixture workspace')
        source = host/'.codex/sessions/2026/01/01/demo.jsonl'; source.parent.mkdir(parents=True)
        source.write_text(json.dumps({'type':'session_meta','payload':{'id':'ssh-smoke-session','cwd':str(project)}})+'\n'+json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'SSH fixture'}]}})+'\n')
        fake_cli = binary/'codex'; fake_cli.write_text('#!/bin/sh\nprintf "REMOTE_PROVIDER_READY %s\\n" "$PWD"\n'); fake_cli.chmod(0o700)
        for name in ('client','hostkey'):
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/name)],check=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); port=listener.getsockname()[1]
        sshd = shutil.which('sshd') or '/usr/sbin/sshd'
        config = root/'sshd.conf'; config.write_text(f'Port {port}\nListenAddress 127.0.0.1\nHostKey {root}/hostkey\nPidFile {root}/sshd.pid\nAuthorizedKeysFile {root}/client.pub\nStrictModes no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\nAllowAgentForwarding no\nAllowTcpForwarding no\nX11Forwarding no\n')
        hostkey = (root/'hostkey.pub').read_text().split(); (root/'known_hosts').write_text(f'[127.0.0.1]:{port} {hostkey[0]} {hostkey[1]}\n')
        client = root/'ssh.conf'; client.write_text(f'Host fixture\n HostName 127.0.0.1\n Port {port}\n IdentityFile {root}/client\n IdentitiesOnly yes\n UserKnownHostsFile {root}/known_hosts\n')
        # Route production SSH arguments through the isolated config and fixture HOME.
        script = binary/'ssh'
        prefix = 'env ' + ' '.join(shlex.quote(k+'='+v) for k,v in {'HOME':str(host),'CODEX_HOME':str(host/'.codex'),'CLAUDE_CONFIG_DIR':str(host/'.claude'),'XDG_CONFIG_HOME':str(host/'.config'),'PATH':str(binary)+':/usr/local/bin:/usr/bin:/bin'}.items()) + ' '
        script.write_text('#!'+sys.executable+'\nimport os,sys\na=sys.argv[1:]\na[-1]='+repr(prefix)+'+a[-1]\nos.execv("/usr/bin/ssh", ["ssh","-F",'+repr(str(client))+']+a)\n'); script.chmod(0o700)
        log = (root/'sshd.log').open('w+')
        daemon = subprocess.Popen([sshd,'-D','-e','-f',str(config)],stdout=log,stderr=log)
        try:
            time.sleep(0.4)
            if daemon.poll() is not None:
                log.seek(0); raise RuntimeError(log.read())
            with patch.dict(os.environ, {'HOME':str(local),'PATH':str(binary)+':/usr/local/bin:/usr/bin:/bin','CODEX_HOME':str(local/'.codex'),'CLAUDE_CONFIG_DIR':str(local/'.claude')}):
                manager=Manager(local,root/'data'); manager.scan(); remote=RemoteLocations(manager)
                library=remote.add('fixture')
                contexts=[c for c in library['contexts'] if c['origin']=='remote']
                assert contexts, library['sshEndpoints']
                context=contexts[0]
                assert remote.read('detail',context['id'])['messages'][0]['content']=='SSH fixture'
                launch=remote.resume(context['id'])
                resumed=subprocess.run([str(script),*launch['args']],stdin=subprocess.DEVNULL,capture_output=True,timeout=15)
                assert resumed.returncode==0 and b'REMOTE_PROVIDER_READY' in resumed.stdout, (resumed.stdout,resumed.stderr)
                destination=local/'workspace'; destination.mkdir()
                result=Teleport(remote).run(context['id'],str(destination),True)
                assert (destination/project.name/'hello.txt').read_text()=='fixture workspace'
                assert remote.context(result['id'])['origin']=='local'
                blocked=subprocess.run([str(script),*launch['args']],stdin=subprocess.DEVNULL,capture_output=True,timeout=15)
                assert blocked.returncode != 0 and b'handed off' in blocked.stdout, (blocked.stdout,blocked.stderr)
                remote.release(context['id'])
                print(json.dumps({'realSSH':True,'discovery':True,'remoteResume':True,'workspaceHandoff':True,'sourceBlocked':True,'release':True}))
        finally:
            daemon.terminate(); daemon.wait(timeout=5); log.close()


if __name__=='__main__':main()
