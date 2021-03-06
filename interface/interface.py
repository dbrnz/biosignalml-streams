import os, sys
import errno
import select
import logging
import urlparse
import multiprocessing
import multiprocessing.sharedctypes
import signal as sighandler

from biosignalml.client import Repository
from biosignalml.units import get_units_uri
from biosignalml.model import BSML
import biosignalml.rdf as rdf

import language
import framestream

VERSION = '0.6.0'

BUFFER_SIZE = 10000

##Stream signals at the given RATE.
##Otherwise all URIs must be for signals from the one BioSignalML recording.

global _debugging
_debugging = False


_interrupted = multiprocessing.Event()

def interrupt(signum, frame):
#============================
  _interrupted.set()


def get_units(units, default=None):
#----------------------------------
  if   units in [None, '']: return default
  elif units[0] == '<':     return units[1:-1]
  else:                     return get_units_uri(units)


class SynchroniseCondition(object):
#==================================

  def __init__(self):
  #------------------
    self._condition = multiprocessing.Condition()
    self._count = multiprocessing.sharedctypes.Value('i', 0)

  def add_waiter(self):
  #--------------------
    self._condition.acquire()
    self._count.value += 1
#    logging.debug('Waiters: %d', self._count.value)
    self._condition.release()

  def wait_for_everyone(self):
  #---------------------------
    self._condition.acquire()
    if self._count.value > 0: self._count.value -= 1
#    logging.debug('Waiting: %d', self._count.value)
    self._condition.notify_all()
    while self._count.value > 0:
      self._condition.wait()
#    logging.debug('Running: %d', self._count.value)
    self._condition.release()


_sender_lock = SynchroniseCondition()


class SignalReader(multiprocessing.Process):
#===========================================

  def __init__(self, signal, output, channel, ratechecker, **options):
  #-------------------------------------------------------------------
    super(SignalReader, self).__init__()
    self._signal = signal
    self._output = output
    self._channel = channel
    self._options = options
    self._ratechecker = ratechecker

  def run(self):
  #-------------
    logging.debug("Running process: %d", self.pid)
    logging.debug("Starting channel %d", self._channel)
    try:
      for ts in self._signal.read(**self._options):
        if _interrupted.is_set(): break
        if ts.is_uniform:
          self._ratechecker.check(ts.rate)
          self._output.put_data(self._channel, ts.data)
        else:
          self._ratechecker.check(None)
          self._output.put_data(self._channel, ts.points)
    except Exception, err:
      logging.error("ERROR: %s", err)
    finally:
      logging.debug("Reader exit... %d", self._channel)
      self._output.put_data(self._channel, None)
      self._signal.close()
      logging.debug("Finished channel %d", self._channel)


class RateChecker(object):
#=========================

  def __init__(self, rate=None):
  #----------------------------
    self._lock = multiprocessing.Lock()
    self._rate = rate

  def check(self, rate):
  #---------------------
    if self._rate is None:
      self._lock.acquire()
      self._rate = rate
      self._lock.release()
    elif self._rate != rate:
      logging.debug("%s != %s", self._rate, rate)
      _interrupted.set()
      raise ValueError("Signal rates don't match")


class OutputStream(multiprocessing.Process):
#===========================================

  def __init__(self, recording, options, signals, dtypes, segment, stream_meta, pipename, binary=False):
  #-----------------------------------------------------------------------------------------------------
    super(OutputStream, self).__init__()
    rate = options.get('rate')
    units = { -1: get_units(options.get('units')) }
    self._signals = [ ]
    repo = recording.repository
    logging.debug("got recording: %s %s", type(recording), str(recording.uri))
    for n, s in enumerate(signals):
      self._signals.append(repo.get_signal(s[0]))
      units[n] = get_units(s[1].get('units'))
    recording.close()
    repo.close()
    logging.debug("got signals: %s", [ (type(s), str(s.uri)) for s in self._signals ])
    self._units = units
    self._rate = rate
    self._dtypes = dtypes
    self._segment = segment
    self._nometadata = not stream_meta
    self._pipename = pipename
    self._binary = binary

  def run(self):
  #-------------

    def send_data(fd, data):
    #-----------------------
      pos = 0
      while pos < len(data):
        ready = select.select([], [fd], [], 0.5)
        if len(ready[1]) == 0: continue
        os.write(fd, data[pos:pos+select.PIPE_BUF])
        pos += select.PIPE_BUF

    logging.debug("Running process: %d", self.pid)
    output = framestream.FrameStream(len(self._signals), self._nometadata, self._binary)
    ratechecker = RateChecker(self._rate)
    readers = [ ]
    fd = os.open(self._pipename, os.O_WRONLY)  # Write will block until there's a reader
    logging.debug("Writing to FD: %d", fd)

    for n, s in enumerate(self._signals):
      readers.append(SignalReader(s, output, n, ratechecker,
                                  rate=self._rate,
                                  units=self._units.get(n, self._units.get(-1)),
                                  dtype=self._dtypes.get(n, self._dtypes.get(-1)),
                                  interval=self._segment, maxpoints=BUFFER_SIZE))
    try:
      for r in readers: r.start()
      starting = True
      for frame in output.frames():
        if starting:
          _sender_lock.wait_for_everyone()
          starting = False
        if _interrupted.is_set(): break
        send_data(fd, frame)
        if not self._binary: send_data(fd, '\n')
        os.fsync(fd)
    except Exception, err:
      logging.error("ERROR: %s", err)
    finally:
      for r in readers:
        if r.is_alive(): r.terminate()
      os.close(fd)
      logging.debug("Finished output: %s", self._pipename)


class InputStream(multiprocessing.Process):
#==========================================

  def __init__(self, rec_uri, options, metadata, signals, dtypes, pipename, binary=False):
  #---------------------------------------------------------------------------------------
    super(InputStream, self).__init__()
    rate = options.get('rate')
    if rate is None: raise ValueError("Input rate must be specified")
    units = { -1: get_units(options.get('units')) }
    self._rate = rate
    self._dtypes = dtypes
    self._pipename = pipename
    self._binary = binary
    self._repo = Repository(rec_uri)
    kwds = dict(label=options.get('label'), description=options.get('desc'))
    self._recording = self._repo.new_recording(rec_uri, **kwds)
    self._recording.save_metadata(metadata.serialise())
    logging.debug("Created %s", self._recording.uri)
    self._signals = [ ]
    for s in signals:
      sig_uri = s[0]
      sigopts = s[1]
      try:
        rec = self._repo.get_recording(sig_uri)
        if rec_uri != rec.uri: raise ValueError("Resource <%s> already in repository" % sig_uri)
      except IOError:
        pass
      kwds = dict(rate=rate, label=sigopts.get('label'), description=sigopts.get('desc'))
      self._signals.append(self._recording.new_signal(sig_uri,
                                                      get_units(sigopts.get('units'), units),
                                                      **kwds))

  def run(self):
  #-------------

    def newdata(n):
    #--------------
      return [ [] for i in xrange(n) ] # Independent lists

    def writedata(signals, data):
    #----------------------------
      for n, s in enumerate(signals):
        s.append(data[n], dtype=self._dtypes.get(n, self._dtypes.get(-1)))

    logging.debug("Running process: %d", self.pid)
    count = 0
    frames = 0
    channels = len(self._signals)
    data = newdata(channels)
    fd = os.open(self._pipename, os.O_RDONLY | os.O_NONBLOCK)
    logging.debug("Reading from FD: %d", fd)

#    for l in self._infile:      ### Binary.... ???
# Wrwp in try ... except ... finally
    buf = ''
    while True:
      ready = select.select([fd], [], [], 0.5)
      if len(ready[0]) == 0: continue
      indata = os.read(fd, 1024)
      if indata == '': break
      buf += indata
      lines = buf.split('\n')
      buf = lines.pop(-1)
      for l in lines:
        frames += 1
        for n, d in enumerate(l.split()[1:]):
          data[n].append(float(d))
        count += 1
        if count >= BUFFER_SIZE:
          writedata(self._signals, data)
          data = newdata(channels)
          count = 0
    logging.debug("Got %d frames", frames)
    os.close(fd)
    if count > 0: writedata(self._signals, data)
    self._recording.duration = frames/self._rate
    self._recording.close()
    self._repo.close()


class DataSource(rdf.Graph):
#===========================

  def __init__(self, recording, segment):
  #--------------------------------------
    rec_uri = rdf.Uri(recording.uri)
    if segment is None:
      super(DataSource, self).__init__(rec_uri)
    else:
      seg_uri = rec_uri.make_uri(True)
      super(DataSource, self).__init__(seg_uri)
      seg = recording.new_segment(seg_uri, segment[0], segment[1])
      seg.save_to_graph(self)


def stream_data(connections, generate='auto', stream_data=True):
#===============================================================

  def get_interval(segment):
  #-------------------------
    if segment is None:
      return None
    times = [segment[0], segment[2]]
    if segment[1] == ':':
      ## ISO durations.... OR seconds...
      return tuple(times)
    elif segment[1] == '-':
      if times[1] < times[0]: raise ValueError("Duration can't be negative")
      return [ times[0], times[1] - times[0] ]

  def create_pipe(name):
  #---------------------
    pipe = os.path.abspath(name)
    try: os.makedirs(os.path.dirname(pipe))
    except OSError, e:
      if e.errno == errno.EEXIST: pass
      else:                       raise
    try: os.mkfifo(pipe, 0600)
    except OSError, e:
      if e.errno == errno.EEXIST: pass
      else:                       raise
    return pipe

  try:
    definitions = list(language.parse(connections))
  except ValueError, msg:
    return msg

  write_streams = [ ]
  read_streams = [ ]
  dtypes = { -1: 'f4' }   ## Don't allow user to specify
  sources = [ ]
  for defn in [ d for d in definitions if d[0] == 'stream' ]:
    rec_uri = defn[1][0][1:-1]
    base = rec_uri + '/'
    repo = Repository(rec_uri)
    recording = repo.get_recording(rec_uri)
    pipe = create_pipe(defn[1][1])
    options = dict(defn[1][2:])
    segment = get_interval(options.pop('segment', None))
    sources.append(DataSource(recording, segment))

    stream_meta = options.pop('stream_meta', False)
    binary = options.pop('binary', False)
    signals = [ (urlparse.urljoin(base, sig[0][1:-1]), dict(sig[1:])) for sig in defn[2]]
    if stream_data:
      write_streams.append(OutputStream(recording, options, signals, dtypes, segment, stream_meta, pipe, binary))
      _sender_lock.add_waiter()

  for defn in [ d for d in definitions if d[0] == 'recording' ]:
    rec_uri = defn[1][0][1:-1]
    base = rec_uri + '/'
    pipe = create_pipe(defn[1][1])
    options = dict(defn[1][2:])
    binary = options.pop('binary', False)
    signals = [ (urlparse.urljoin(base, sig[0][1:-1]), dict(sig[1:])) for sig in defn[2]]
    turtle = defn[3].strip()
    if generate == 'none' and turtle == '':
      metadata = None
    else:
      metadata = rdf.Graph()
      if (generate == 'all'
       or generate == 'auto' and turtle == ''):  # add sources
        for s in sources:
          metadata.append(rdf.Statement(rec_uri, rdf.DCT.source, s.uri))
          metadata.add_statements(s.as_stream())
      if turtle != '':
        # get from rdf plus BSML ??
        ## rdf.NAMESPACES.
        PREFIXES = {
          'bsml': BSML.URI,
          'dct':  'http://purl.org/dc/terms/',
           }
        # add base and standard prefixes, parse and add turtle
        metadata.parse_string('\n'.join([ '@base <%s> .' % base ]
                                      + [ '@prefix %s: <%s> .' % (p, u) for (p, u) in PREFIXES.iteritems() ]
                                      + [ turtle ]), format=rdf.Format.TURTLE, base=base)
    if stream_data:
      read_streams.append(InputStream(rec_uri, options, metadata, signals, dtypes, pipe, binary))

  sighandler.signal(sighandler.SIGINT, interrupt)
  try:    # Start all readers before streaming anything
    for s in read_streams: s.start()
    for s in write_streams: s.start()
  except Exception, msg:
    _interrupted.set()
    if _debugging: raise
    logging.error("ERROR: %s", msg)
    return msg
  finally:
    for s in (write_streams + read_streams):
      logging.debug("%s %s %s %s", s, s.is_alive(), s.pid, s.exitcode)
      if s.is_alive(): s.join(0.5)


if __name__ == '__main__':
#=========================

  import docopt

  multiprocessing.freeze_support()
  # We lock up with ^C interrupt unless multiprocessing has a logger
  logger = multiprocessing.log_to_stderr()
  logger.setLevel(logging.ERROR)

  LOGFORMAT = '%(asctime)s %(levelname)8s %(processName)s: %(message)s'
  logging.basicConfig(format=LOGFORMAT)

  usage = """Usage:
  %(prog)s [options] [--metadata=(auto | none | all)] (CONNECTION_DEFINITION | -f FILE)
  %(prog)s (-h | --help)

Connect a BioSignalML repository with telemetry streams,
using definitions from either the command line or a file.

Options:

  -h --help       Show this text and exit.

  -f FILE --file=FILE
                  Take connection information from FILE.

  -d --debug      Enable debugging.

  --metadata=(auto | none | all)
                  Determines how additional metadata is generated for new
                  recordings. Any metadata specified via the recording's
                  definition is always used. [default: auto]

                  'auto':  If no metadata is specified, the dct:source
                           property is used to associate the new recording
                           with all source recordings.

                  'none':  No addititional metadata is generated.

                  'all':   The dct:source property is used to associate
                           the new recording with all source recordings,
                           regardless of metadata being specified.

  -n --no-stream  Parse options and connection definitions without
                  actually sending or receiving data.

  -v --version    Show version and exit.

  """

  args = docopt.docopt(usage % { 'prog': sys.argv[0] } )

  if args['--debug']:
    _debugging = True
    logging.getLogger().setLevel(logging.DEBUG)
  logging.debug("ARGS: %s", args)

  if args['--version']:
    sys.exit('%s Version %s' % (sys.argv[0], VERSION))

  if args['--metadata'] not in ['auto', 'none', 'all']:
    sys.exit("'metadata' option must be one of 'auto', 'none', or 'all'")

  if args['--file'] is not None:
    with open(args['--file']) as f:
      definitions = f.read()
  elif args['CONNECTION_DEFINITION'] is not None:
    definitions = args['CONNECTION_DEFINITION']

  sys.exit(stream_data(definitions, args['--metadata'], not args['--no-stream']))
