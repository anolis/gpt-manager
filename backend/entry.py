"""Packaged backend entry point; Python is bundled, never required from the user."""
import runpy
import sys

if __name__ == '__main__':
    mode = sys.argv.pop(1) if len(sys.argv) > 1 else ''
    if mode == 'rpc':
        runpy.run_module('rpc', run_name='__main__')
    elif mode == 'usage':
        from usage_capture import main
        main()
    elif mode == 'lock':
        from session_lock import resume_locked
        try: sys.exit(resume_locked(*sys.argv[1:5], sys.argv[5:]))
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr); sys.exit(1)
    elif mode == 'terminal' and sys.platform != 'win32':
        from terminal import main
        main()
    else:
        raise SystemExit('Unknown backend mode')
