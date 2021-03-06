import functools as ft
import json
import os
import re
import time
import threading
from concurrent import futures
from contextlib import contextmanager
from imaplib import CRLF

from . import log

DEBUG = int(os.environ.get('IMAP_DEBUG', 0))
commands = {}


class Error(Exception):
    def __repr__(self):
        return '%s.%s: %s' % (__name__, self.__class__.__name__, self.args)


def fn_desc(func, *a, **kw):
    args = ', '.join(
        [repr(i) for i in a] +
        (['**%r' % kw] if kw else [])
    )
    maxlen = 80
    if len(args) > maxlen:
        args = '%s...' % args[:maxlen]
    name = getattr(func, 'name', func.__name__)
    return '%s(%s)' % (name, args)


def log_time(func):
    @ft.wraps(func)
    def inner(*a, **kw):
        start = time.time()
        try:
            return func(*a, **kw)
        finally:
            desc = fn_desc(func, *a, **kw)
            log.debug('## %s: done for %.2fs', desc, time.time() - start)
    return inner


def fn_lock(func):
    @ft.wraps(func)
    def inner(con, *a, **kw):
        with con.lock:
            return log_time(func)(con, *a, **kw)
    return inner


def check(res):
    typ, data = res
    if typ != 'OK':
        raise Error(typ, data)
    return data


def command(*, name=None, lock=True, writable=False, dovecot=False):
    def inner(func):
        if name:
            func.name = name
        else:
            func.name = func.__name__

        if lock:
            func = fn_lock(func)

        commands[func] = {'writable': writable, 'dovecot': dovecot}
        return func
    return inner


class Ctx:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return str(self)

    def __str__(self):
        return '%s[%r]' % (self.name, self.box())


def client(name, connect, *, writable=False, dovecot=False, debug=DEBUG):
    def start():
        con = connect()
        con.debug = debug
        con.lock = threading.RLock()
        con.dovecot = dovecot
        con.new = new
        return con

    def new():
        con = start()
        if ctx.box():
            con.select(ctx.box(), con.is_readonly)
        return con

    con = start()
    ctx = Ctx(name)
    ctx.logout = con.logout
    ctx.box = lambda: getattr(con, 'current_box', None)
    ctx.str = lambda: '%s[%r]' % (ctx.__class__.__name__, ctx.box())

    for cmd, opts in commands.items():
        if not dovecot and opts['dovecot']:
            continue
        elif not writable and opts['writable']:
            continue
        setattr(ctx, cmd.name, ft.partial(cmd, con))
    return ctx


@contextmanager
def _cmd(con, name):
    tag = con._new_tag()

    def start(args):
        if isinstance(args, str):
            args = args.encode()
        return con.send(b'%s %s %s' % (tag, name.encode(), args))
    yield tag, start, lambda: con._command_complete(name, tag)


def _mdkey(key):
    if not key.startswith('/private'):
        key = '/private/%s' % key
    return key


@command(dovecot=True, writable=True)
def setmetadata(con, box, key, value):
    key = _mdkey(key)
    with _cmd(con, 'SETMETADATA') as (tag, start, complete):
        args = '%s (%s %s)' % (box, key, json.dumps(value))
        start(args.encode() + CRLF)
        typ, data = complete()
        return check(con._untagged_response(typ, data, 'METADATA'))


@command(dovecot=True)
def getmetadata(con, box, key):
    key = _mdkey(key)
    with _cmd(con, 'GETMETADATA') as (tag, start, complete):
        args = '%s (%s)' % (box, key)
        start(args.encode() + CRLF)
        typ, data = complete()
        return check(con._untagged_response(typ, data, 'METADATA'))


@command(dovecot=True, writable=True)
def multiappend(con, box, msgs):
    if not msgs:
        return

    with _cmd(con, 'APPEND') as (tag, start, complete):
        send = start
        for date_time, flags, msg in msgs:
            args = (' (%s) %s %s' % (flags, date_time, '{%s}' % len(msg)))
            if send == start:
                args = '%s %s' % (box, args)
            send(args.encode() + CRLF)
            send = con.send
            while con._get_response():
                if con.tagged_commands[tag]:   # BAD/NO?
                    return tag
            con.send(msg)
        con.send(CRLF)
        return check(complete())


@command(dovecot=True)
def thread(con, *criteria):
    res = check(con.uid('THREAD', *criteria))
    return parse_thread(res[0].decode())


@command(dovecot=True)
def sort(con, fields, *criteria, charset='UTF-8'):
    return check(con.uid('SORT', fields, charset, *criteria))


@command(name='list')
def xlist(con, folder='""', pattern='*'):
    return check(con.list(folder, pattern))


@command()
def select(con, box, readonly=True):
    con.current_box = box.decode() if isinstance(box, bytes) else box
    return check(con.select(box, readonly))


@command(lock=False)
def select_tag(con, tag, readonly=True):
    if isinstance(tag, str):
        tag = tag.encode()
    folder = None
    folders = xlist(con)
    for f in folders:
        if not re.search(br'^\([^)]*?%s' % re.escape(tag), f):
            continue
        folder = f.rsplit(b' "/" ', 1)[1]
        break
    if folder is None:
        raise ValueError('No folder with tag: %s\n%s' % (tag, folders))
    return select(con, folder, readonly)


@command()
def status(con, box, fields):
    box = con.current_box if box is None else box
    return check(con.status(box, fields))


@command()
def search(con, *criteria):
    return check(con.uid('SEARCH', None, *criteria))


@command(writable=True)
def append(con, box, flags, date_time, msg):
    return check(con.append(box, flags, date_time, msg))


@command(writable=True)
def expunge(con):
    return check(con.expunge())


@command(lock=False)
def fetch(con, uids, fields):
    if not isinstance(uids, (str, bytes)):
        @ft.wraps(fetch)
        def inner(uids, *args):
            uids = ','.join(uids)
            new = con.new()
            with new:
                return fetch(new, uids, *args)
            return fetch(con, uids, *args)

        res = partial_uids(delayed_uids(inner, uids, fields))
        res = ([] if len(i) == 1 and i[0] is None else i for i in res)
        return sum(res, [])

    with con.lock:
        return check(con.uid('FETCH', uids, fields))


@command(lock=False, writable=True)
def store(con, uids, cmd, flags):
    if not isinstance(uids, (str, bytes)):
        @ft.wraps(store)
        def inner(uids, *args):
            return store(con, ','.join(uids), *args)

        res = partial_uids(delayed_uids(inner, uids, cmd, flags))
        res = ([] if len(i) == 1 and i[0] is None else i for i in res)
        return sum(res, [])

    with con.lock:
        return check(con.uid('STORE', uids, cmd, flags))


def parse_thread(line):
    if isinstance(line, bytes):
        line = line.decode()

    threads = []
    uids = []
    uid = ''
    opening = 0
    for i in line:
        if i == '(':
            opening += 1
        elif i == ')':
            if uid:
                uids.append(uid)
                uid = ''

            opening -= 1
            if opening == 0:
                threads.append(uids)
                uids = []
        elif i == ' ':
            uids.append(uid)
            uid = ''
        else:
            uid += i
    return threads


def pack_uids(uids):
    uids = sorted(int(i) for i in uids)
    result = ''
    for i, uid in enumerate(uids):
        if i == 0:
            result += str(uid)
        elif uid - uids[i-1] == 1:
            if len(uids) == (i + 1):
                if not result.endswith(':'):
                    result += ':'
                result += str(uid)
            elif result.endswith(':'):
                pass
            else:
                result += ':'
        elif result.endswith(':'):
            result += '%d,%d' % (uids[i-1], uid)
        else:
            result += ',%s' % uid
    return result


def delayed_uids(func, uids, *a, **kw):
    @ft.wraps(func)
    def inner(uids, num=None):
        num = '%s#' % num if num else ''
        log.info('## %s%s: %s uids', num, inner.desc, len(uids))
        start = time.time()
        try:
            res = func(uids, *a, **kw)
            log.info(
                '## %s%s: done for %.2fs',
                num, inner.desc, time.time() - start
            )
        except Exception as e:
            log.exception('## %s%s: %r' % (num, inner.desc, e))
            raise
        return res

    inner.uids = list(uids)
    inner.desc = fn_desc(func, 'uids', *a, **kw)
    return inner


def partial_uids(delayed, size=5000, threads=10):
    uids = delayed.uids
    if not uids:
        return []
    elif len(uids) <= size:
        return [delayed(uids)]

    jobs = []
    with futures.ThreadPoolExecutor(threads) as pool:
        for i in range(0, len(uids), size):
            num = '%02d' % (i // size + 1)
            few = uids[i:i+size]
            jobs.append(pool.submit(delayed, few, num))
    return [f.result() for f in jobs]
