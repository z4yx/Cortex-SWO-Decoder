#!/usr/bin/python3
#
#  This program is free software. It comes without any 
#  warranty, to the extent permitted by applicable law.
#

import select
import socket
import sys
import termios
import time

class Stream:
    """
    Stream of messages for one of the 32 ITM channels.
    
    ITM Trace messages can be output on one of 32 channels. The stream class 
    contains a byte buffer for one of these channels. Once a newline character
    is received, the buffer is dumped to stdout. Also, there is an optional
    string that is prepended to the start of each line. This is useful for
    using different channels for different logging levels.
    
    For example, one might use channel 0 for info messages, channel 1 for
    warnings, and channel 2 for errors. One might like to use "INFO: ",
    "WARNING: ", and "ERROR: " as the headers for these channels. The debugger
    can enable and disable these channels on startup if you want to only see
    error messages. This would actually prevent the info and warning messages
    from being generated by the processor, which will save time in the code
    because ITM routines are blocking.
    
    Each stream also has the option to echo to the GDB console. Simply pass
    the socket connected to the Tcl server to the constructor
    
    """
    
    # Max number of characters for a stream before a newline needs to occur
    MAX_LINE_LENGTH = 1024
    
    def __init__(self, id, header = '', tcl_socket = None):
        self.id = id;
        self._buffer = []
        self._header = header
        self.tcl_socket = tcl_socket
        
    def add_char(self, c):
        if len(self._buffer) >= self.MAX_LINE_LENGTH:
            self._output('SWO_PARSER.PY WARNING: stream ' + str(self.id) +
                    ' received ' + str(self.MAX_LINE_LENGTH) +
                    ' bytes without receiving a newline. Did you forget one?')
            self._output(self._header + ''.join(self._buffer) + c)
            self._buffer = []
            return
        
        if c == '\n':
            self._output(self._header + ''.join(self._buffer))
            self._buffer = []
            return
            
        self._buffer.append(c)
        
    def add_chars(self, s):
        for c in s:
            self.add_char(c)
            
    def _output(self, s):
        print(s)
        if self.tcl_socket is not None:
            self.tcl_socket.sendall(b'puts "' + s.encode('utf-8') + b'"\r\n\x1a')
        
        
class StreamManager:
    """
    Manages up to 32 byte streams.
    
    This class contains a dictionary of streams indexed by their stream id. It
    is responsible for parsing the incoming data, and forwarding the bytes to
    the correct stream.
    
    """
    def __init__(self):
        self.streams = dict()
        self._itmbuffer = b''
        
    def add_stream(self, stream):
        self.streams[stream.id] = stream
        
    def parse_tcl(self, line):
        r"""
        When OpenOCD is configured to output the trace data over the Tcl
        server, it periodically outputs a string (terminated with \r\n) that
        looks something like this:
        
        type target_trace data 01480165016c016c016f0120015401720161016301650121010a
        
        The parse_tcl method turns this into the raw ITM bytes and sends it to
        parse_itm_bytes.
        
        """
        if (line.startswith(b'type target_trace data ') and 
            line.endswith(b'\r\n')
            ):
            itm_bytes = int(line[23:-2],16).to_bytes(len(line[23:-2])//2,
                                               byteorder='big')
            self.parse_itm_bytes(itm_bytes)
                    
    def parse_itm_bytes(self, bstring):
        """
        Parses ITM packets based on the format discription from ARM
        http://infocenter.arm.com/help/index.jsp?topic=/com.arm.doc.ddi0314h/Chdbicbg.html
        
        """
        
        bstring = self._itmbuffer + bstring
        self._itmbuffer = b''
        
        while len(bstring) > 0:
            header = bstring[0]
            # The third bit of a header must be zero, and the last two bits
            # can't be zero.
            if header & 0x04 != 0 or header & 0x03 == 0:
                bstring = bstring[1:]
                continue
                                
            payload_size = 2**(header & 0x03 - 1)
            stream_id = header >> 3
            
            if payload_size >= len(bstring):
                self._itmbuffer = bstring
                return
                
            if stream_id in self.streams:
                s = bstring[1:payload_size+1].decode('ascii', 'ignore')
                self.streams[stream_id].add_chars(s)
            
            bstring = bstring[payload_size+1:]
   
def raw_termios():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] = new[3] & ~termios.ICANON & ~termios.ECHO & ~termios.IEXTEN #& ~termios.ISIG 
    new[6][termios.VMIN] = 0
    new[6][termios.VTIME] = 0
    return fd, old, new

#### Main program ####

# Set up the socket to the OpenOCD Tcl server
CPU_CLK = 80000000
HOST = 'localhost'
PORT = 6666
if len(sys.argv) > 1:
    CPU_CLK = int(sys.argv[1])
    assert CPU_CLK > 0
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcl_socket:
    tcl_socket.connect((HOST, PORT))
    # tcl_socket.settimeout(0)
    def send_tcl(tcl):
        tcl_socket.sendall(tcl.encode('ascii') + b'\n\x1a')
    
    # Create a stream manager and add three streams
    streams = StreamManager()
    streams.add_stream(Stream(0, '', tcl_socket))
    streams.add_stream(Stream(1, 'WARNING: '))
    streams.add_stream(Stream(2, 'ERROR: ', tcl_socket))

    term_fd, term_old, term_raw = raw_termios()
    
    # Enable the tcl_trace output
    send_tcl('init')
    send_tcl('tpiu config internal - uart off ' + str(CPU_CLK))
    send_tcl('itm ports on')
    send_tcl('tcl_trace on')

    print("CPU clock:", CPU_CLK/10**6, "MHz")
    print("Ctrl-F: (re-)program the flash")
    print("Ctrl-R: reset the chip")

    try:
        termios.tcsetattr(term_fd, termios.TCSADRAIN, term_raw)
        tcl_buf = b''
        while True:
            # Wait for new data from the socket
            ready = select.select([tcl_socket, sys.stdin], [], [])[0]
            if tcl_socket in ready:
                data = tcl_socket.recv(1024)
                if len(data) == 0:
                    print("Connection Closed")
                    break
                tcl_buf = tcl_buf + data

                # Tcl messages are terminated with a 0x1A byte
                temp = tcl_buf.split(b'\x1a',1)
                while len(temp) == 2:
                    # Parse the Tcl message
                    streams.parse_tcl(temp[0])
                    
                    # Remove that message from tcl_buf and grab another message from
                    # the buffer if the is one
                    tcl_buf = temp[1]
                    temp = tcl_buf.split(b'\x1a',1)
            if sys.stdin in ready:
                key = 64 + ord(sys.stdin.read()[0])
                if key == ord('F'): # Ctrl-F
                    print("Programming...")
                    send_tcl('reset halt')
                    send_tcl('flash write_image erase main.bin 0x08000000')
                    send_tcl('reset')
                elif key == ord('R'): # Ctrl-R
                    print("Resetting...")
                    send_tcl('reset')
                elif key == ord('L'): # Ctrl-L
                    send_tcl('reset halt')
                    send_tcl('stm32l4x lock 0')
                    send_tcl('reset')
                elif key == ord('U'): # Ctrl-U
                    send_tcl('reset halt')
                    send_tcl('stm32l4x unlock 0')
                    send_tcl('reset')
    except KeyboardInterrupt:
        print('Terminating...')
    finally:
        termios.tcsetattr(term_fd, termios.TCSADRAIN, term_old)
        # Turn off the trace data before closing the port
        send_tcl('tcl_trace off')
