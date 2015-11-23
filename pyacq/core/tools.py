from pyqtgraph.Qt import QtCore, QtGui
from pyqtgraph.util.mutex import Mutex
import weakref
import numpy as np
from collections import OrderedDict
import logging

from .node import Node, register_node_type
from .stream import OutputStream, InputStream

import time


class ThreadPollInput(QtCore.QThread):
    """Thread that polls an InputStream in the background and emits a signal
    when data is received.
    
    This class is used where low-latency response to data is needed within a Qt
    main thread (because polling from the main thread with QTimer either 
    introduces too much latency or consumes too much CPU).
    
    The `process_data()` method may be reimplemented to define other behaviors.
    """
    new_data = QtCore.Signal(int,object)
    def __init__(self, input_stream, timeout=200, parent=None):
        QtCore.QThread.__init__(self, parent)
        self.input_stream = weakref.ref(input_stream)
        self.timeout = timeout
        
        self.running = False
        self.running_lock = Mutex()
        self._pos = None
        
    def run(self):
        with self.running_lock:
            self.running = True
        
        while True:
            with self.running_lock:
                if not self.running:
                    break
                if self.input_stream() is None:
                    logging.info("ThreadPollInput has lost InputStream")
                    break
            ev = self.input_stream().poll(timeout=self.timeout)
            if ev>0:
                self._pos, data = self.input_stream().recv()
                self.process_data(self._pos, data)
    
    def process_data(self, pos, data):
        # This can be override to chnage behavior
        self.new_data.emit(self._pos, data)
    
    def stop(self):
        with self.running_lock:
            self.running = False
    
    def pos(self):
        return self._pos

class ThreadPollOutput(ThreadPollInput):
    """    
    Thread that monitors an OutputStream in the background and emits a Qt signal
    when data is sent.

    Like ThreadPollInput, this class can be used where low-latency response to data
    is needed within a Qt main thread (because polling from the main thread with
    QTimer either introduces too much latency or consumes too much CPU).

    The `process_data()` method may be reimplemented to define other behaviors.
    
    This is class also create internally its own `InputStream`.
    And pull it the same way than ThreadPollInput.
    """
    def __init__(self, output_stream, **kargs):
        self.instream = InputStream()
        self.instream.connect(output_stream)
        ThreadPollInput.__init__(self, self.instream, **kargs)


class ThreadStreamConverter(ThreadPollInput):
    """Thread that polls for data on an input stream and converts the transfer
    mode or time axis of the data before relaying it through its output.
    """
    def __init__(self, input_stream, output_stream, conversions,timeout=200, parent=None):
        ThreadPollInput.__init__(self, input_stream, timeout=timeout, parent=parent)
        self.output_stream = weakref.ref(output_stream)
        self.conversions = conversions
    
    def process_data(self, pos, data):
        if 'transfermode' in self.conversions and self.conversions['transfermode'][0]=='sharedarray':
            data = self.input_stream().get_array_slice(self, pos, None)
        #~ if 'timeaxis' in self.conversions:
            #~ data = data.swapaxes(*self.conversions['timeaxis'])
        self.output_stream().send(pos, data)


class StreamConverter(Node):
    """
    A Node that converts one stream type to another.
    
    For instance:
    
    * convert transfer mode 'plaindata' to 'sharedarray'. (to get a local long buffer)
    * convert dtype 'int32' to 'float64'
    * change timeaxis 0 to 1 (in fact a transpose)
    * ...
    
    Usage::
    
        conv = StreamConverter()
        conv.configure()
        conv.input.connect(someinput)
        conv.output.configure(someotherspec)
        conv.initialize()
        conv.start()
    
    
    """
    _input_specs = {'in': {}}
    _output_specs = {'out': {}}
    
    def __init__(self, **kargs):
        Node.__init__(self, **kargs)
    
    def _configure(self, **kargs):
        pass
    
    def _initialize(self):
        self.conversions = {}
        # check convertion
        for k in self.input.params:
            if k in ('port', 'protocol', 'interface', 'dtype'):
                continue  # the OutputStream/InputStream already do it
            
            old, new = self.input.params.get(k, None), self.output.params.get(k, None)
            if old != new and old is not None:
                self.conversions[k] = (old, new)
                
        # DO some check ???
        # if 'shape' in self.conversions:
        #    assert 'timeaxis' in self.conversions        
        self.thread = ThreadStreamConverter(self.input, self.output, self.conversions)
    
    def _start(self):
        self.thread.start()

    def _stop(self):
        self.thread.stop()
        self.thread.wait()
    
    def _close(self):
        pass

register_node_type(StreamConverter)



class ThreadSplitter(ThreadPollInput):
    def __init__(self, input_stream, outputs_stream, output_channels, timeout=200, parent=None):
        ThreadPollInput.__init__(self, input_stream, timeout=timeout, parent=parent)
        self.outputs_stream = weakref.WeakValueDictionary()
        self.outputs_stream.update(outputs_stream)
        self.output_channels = output_channels
    
    def process_data(self, pos, data):
        if data is None:
            #sharred_array case
            data =  self.input_stream().get_array_slice(pos, None)
        
        for k , chans in self.output_channels.items():
            self.outputs_stream[k].send(pos, data[:, chans])


class ChannelSplitter(Node):
    """
    ChannelSplitter take a multi-channel input signal stream and splits it
    into several sub streams.
    
    Usage::
    
        splitter = StreamSplitter()
        splitter.configure(output_channels = { 'out0' : [1,2,3], 'out1' : [4,5,6] })
        splitter.input.connect(someinput)
        for output in splitter.outputs.values():
            output.configure(someotherspec)
        splitter.initialize()
        splitter.start()
        
    """
    _input_specs = {'in': {}}
    _output_specs = {}  # done dynamically in _configure
    
    def __init__(self, **kargs):
        Node.__init__(self, **kargs)
    
    def _configure(self, output_channels = {}, output_timeaxis = 'same'):
        """
        Params
        -----------
        output_channels: dict of list
            This contain a dict of sub channel list.
            Each key will be the name of each output.
        output_timeaxis: int or 'same'
            The output timeaxis is set here.
        """
        self.output_channels = output_channels
        self.output_timeaxis = output_timeaxis
    
    def after_input_connect(self, inputname):
        if self.output_timeaxis == 'same':
            self.output_timeaxis = self.input.params['timeaxis']
        timeaxis = self.output_timeaxis
        
        n =  self.input.params['nb_channel']
        self.outputs = OrderedDict()
        for k, chans in self.output_channels.items():
            assert min(chans)>=0 and max(chans)<n, 'output_channels do not match channel count {}'.format(n)

            stream_spec = dict(streamtype='analogsignal', dtype = self.input.params['dtype'],
                                                sample_rate = self.input.params['sample_rate'])
            stream_spec['shape'] = None
            stream_spec['port'] = '*'
            stream_spec['nb_channel'] = len(chans)
            stream_spec['timeaxis'] = timeaxis
            if timeaxis==0:
                stream_spec['shape'] = (-1, len(chans))
            else:
                stream_spec['shape'] = (len(chans), -1)
            
            output = OutputStream(spec=stream_spec)
            self.outputs[k] = output
    
    def _initialize(self):
        self.thread = ThreadSplitter(self.input, self.outputs, self.output_channels)
    
    def _start(self):
        self.thread.start()

    def _stop(self):
        self.thread.stop()
        self.thread.wait()
    
    def _close(self):
        pass

register_node_type(ChannelSplitter)
