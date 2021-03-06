import logging

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    datefmt='%Y-%m-%d %H:%M:%S%Z',
    format='[%(asctime)s][%(thread)s] %(levelname).3s %(message)s'
)
