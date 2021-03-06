"""Mailur CLI

Usage:
  mailur -l<login> gmail <username> <password> [--parse -t<threads> -b<batch>]
  mailur -l<login> parse [<criteria> -t<threads> -b<batch>]
  mailur -l<login> threads [<criteria>]

Options:
  -h --help     Show this screen.
  --version     Show version.
  -l <login>    Local user (for dovecot).
  -b <batch>    Batch size [default: 500].
  -t <threads>  Amount of threads for thread pool [default: 8].
"""
from docopt import docopt

from . import gmail, local, imap


def main(args):
    local.USER = args['-l']
    opts = {
        'batch': int(args.get('-b')),
        'threads': int(args.get('-t')),
    }
    if args['gmail']:
        gmail.USER = args['<username>']
        gmail.PASS = args['<password>']
        uids = gmail.fetch(**opts)
        if args.get('--parse'):
            criteria = 'UID %s' % imap.pack_uids(uids) if uids else None
            local.parse(criteria, **opts)
    elif args['parse']:
        local.parse(args.get('<criteria>'), **opts)
    elif args['threads']:
        local.update_threads(criteria=args.get('<criteria>'))


if __name__ == '__main__':
    args = docopt(__doc__, version='Mailur 0.3')
    try:
        main(args)
    except KeyboardInterrupt:
        raise SystemExit('^C')
