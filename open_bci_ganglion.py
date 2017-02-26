"""
Core OpenBCI object for handling connections and samples from the gnaglion board.

EXAMPLE USE:

def handle_sample(sample):
  print(sample.channels)

board = OpenBCIBoard()
board.start(handle_sample)

TODO: Pick between several boards
TODO: support impedance
TODO: support accelerometer with n / N codes

"""
import struct
import numpy as np
import time
import timeit
import atexit
import logging
import threading
import sys
import pdb
import glob
# local bluepy should take precedence
import sys
sys.path.insert(0,"bluepy/bluepy")
from btle import Scanner, DefaultDelegate, Peripheral

SAMPLE_RATE = 100.0  # Hz
scale_fac_uVolts_per_count = 1200 * 8388607.0 * 1.5 * 51.0;

# service for communication, as per docs
BLE_SERVICE = "fe84"
# characteristics of interest
BLE_CHAR_RECEIVE = "2d30c082f39f4ce6923f3484ea480596"
BLE_CHAR_SEND = "2d30c083f39f4ce6923f3484ea480596"
BLE_CHAR_DISCONNECT = "2d30c084f39f4ce6923f3484ea480596"

'''
#Commands for in SDK http://docs.openbci.com/Hardware/08-Ganglion_Data_Forma

command_stop = "s";
command_startBinary = "b";
'''

class OpenBCIBoard(object):
  """

  Handle a connection to an OpenBCI board.

  Args:
    port: MAC address of the Ganglion Board. "None" to attempt auto-detect.
    baud, filter_data, daisy: Not used, for compatibility with v3
  """

  def __init__(self, port=None, baud=0, filter_data=False,
    scaled_output=True, daisy=False, log=True, timeout=None):
    self.log = log # print_incoming_text needs log
    self.streaming = False
    self.timeout = timeout

    print("Looking for Ganglion board")
    if port == None:
      port = self.find_port()   
    self.port = port # find_port might not return string

    print ("Init BLE connection with MAC: " + self.port)
    print ("NB: if it fails, try with root privileges.")
    self.gang = Peripheral(port, 'random') # ADDR_TYPE_RANDOM

    print ("Get mainservice...")
    self.service = self.gang.getServiceByUUID(BLE_SERVICE)
    print ("Got:" + str(self.service))
    
    print ("Get characteristics...")
    self.char_read = self.service.getCharacteristics(BLE_CHAR_RECEIVE)[0]
    print ("receive, properties: " + str(self.char_read.propertiesToString()) + ", supports read: " + str(self.char_read.supportsRead()))

    self.char_write = self.service.getCharacteristics(BLE_CHAR_SEND)[0]
    print ("write, properties: " + str(self.char_write.propertiesToString()) + ", supports read: " + str(self.char_write.supportsRead()))

    self.char_discon = self.service.getCharacteristics(BLE_CHAR_DISCONNECT)[0]
    print ("disconnect, properties: " + str(self.char_discon.propertiesToString()) + ", supports read: " + str(self.char_discon.supportsRead()))

    print("Connection established")

    #wait for device to be ready, just in case
    time.sleep(1)

    self.streaming = False
    self.scaling_output = scaled_output
    self.eeg_channels_per_sample = 4 # number of EEG channels per sample *from the board*
    self.read_state = 0
    self.log_packet_count = 0
    self.packets_dropped = 0

    #Disconnects from board when terminated
    atexit.register(self.disconnect)

  def find_port(self):
    """DetectsGanglion board MAC address -- if more than 1 around, will select first. Needs root privilege."""

    print("Try to detect Ganglion MAC address. NB: Turn on bluetooth and run as root for this to work!")
    scan_time = 5
    print("Scanning for 5 seconds nearby devices...")

  #   From bluepy example
    class ScanDelegate(DefaultDelegate):
      def __init__(self):
        DefaultDelegate.__init__(self)

      def handleDiscovery(self, dev, isNewDev, isNewData):
        if isNewDev:
          print ("Discovered device: " + dev.addr)
        elif isNewData:
          print ("Received new data from: " + dev.addr)
  
    scanner = Scanner().withDelegate(ScanDelegate())
    devices = scanner.scan(scan_time)

    nb_devices = len(devices)
    if nb_devices < 1:
      print("No BLE devices found. Check connectivity.")
      return ""
    else:
      print("Found " + str(nb_devices) + ", detecting Ganglion")
      list_mac = []
      list_id = []
  
      for dev in devices:
        # "Ganglion" should appear inside the "value" associated to "Complete Local Name", e.g. "Ganglion-b2a6"
        for (adtype, desc, value) in dev.getScanData():
          if desc == "Complete Local Name" and   value.startswith("Ganglion"): 
            list_mac.append(dev.addr)
            list_id.append(value)
            print("Got Ganglion: " + value + ", with MAC: " + dev.addr)
            break
    nb_ganglions = len(list_mac)
  
    if nb_ganglions < 1:
      print("No Ganglion found ;(")
      raise OSError('Cannot find OpenBCI Ganglion MAC address')

    if nb_ganglions > 1:
      print("Found " + str(nb_ganglions) + ", selecting first")

    print("Selecting MAC address " + list_mac[0] + " for " + list_id[0])
    return list_mac[0]
    
  def ser_write(self, b):
    """Access serial port object for write""" 
    self.char_write.write(b)

  def ser_read(self):
    """Access serial port object for read""" 
    return self.char_read.read()
    
  def getSampleRate(self):
      return SAMPLE_RATE
  
  def getNbEEGChannels(self):
      return self.eeg_channels_per_sample
  
  def getNbAUXChannels(self):
    """Not implemented on the Ganglion"""
    return 0 

  def start_streaming(self, callback, lapse=-1):
    """
    Start handling streaming data from the board. Call a provided callback
    for every single sample that is processed

    Args:
      callback: A callback function -- or a list of functions -- that will receive a single argument of the
          OpenBCISample object captured.
    """
    if not self.streaming:
      self.char_write.write(b'b')
      self.streaming = True

    start_time = timeit.default_timer()

    # Enclose callback funtion in a list if it comes alone
    if not isinstance(callback, list):
      callback = [callback]
    

    #Initialize check connection
    self.check_connection()

    while self.streaming:

      # read current sample
      # FIXME: several samples per packet
      sample = self._read_serial_binary()
      #for call in callback:
      #    call(sample)
      
      if(lapse > 0 and timeit.default_timer() - start_time > lapse):
        self.stop();
      if self.log:
        self.log_packet_count = self.log_packet_count + 1;
  
  
  """
    PARSER:
    Parses incoming data packet into OpenBCISample -- see docs.
    FIXME: use buffer to account for missed / double packets?
  """
  def _read_serial_binary(self):

    print("reading packet")
    packet = self.ser_read()
    # poor handling of errors...
    if not packet:
      self.warn('Device appears to be stalling.')
      return
    if len(packet) != 20:
      self.warn('Wrong packet size, ' + str(len(packet)) + ' instead of 20 bytes')
      return

    for b in packet:
      unpac = struct.unpack('B', b)
      print unpac

    start_byte = struct.unpack('B', packet[0])
    print(str(start_byte))
    # Raw uncompressed
    if start_byte == 0:
      print ("Raw uncompressed")
    # 18-bit compression with Accelerometer
    elif start_byte >= 1 and start_byte <= 100:
      print("18-bit compression with Accelerometer") 
    # 19-bit compression without Accelerometer
    elif start_byte >=101 and start_byte <= 200:
      print("19-bit compression without Accelerometer")
    # Impedance Channel
    elif start_byte >= 201 and start_byte <= 205:
      print("Impedance Channel")  
    # Part of ASCII
    elif start_byte == 206:
      print("ASCII message")
      print (packet)
    # End of ASCII message
    elif start_byte == 207:
      print ("End of ASCII message")
      print (packet)
      print ("----")
    else:
      self.warn("Unknown type of packet: " + str(start_byte))
    

    if False:
          #3 byte ints
          literal_read = read(3)

          unpacked = struct.unpack('3B', literal_read)

          #3byte int in 2s compliment
          if (unpacked[0] >= 127):
            pre_fix = bytes(bytearray.fromhex('FF')) 
          else:
            pre_fix = bytes(bytearray.fromhex('00'))

          literal_read = pre_fix + literal_read;

          #unpack little endian(>) signed integer(i) (makes unpacking platform independent)
          myInt = struct.unpack('>i', literal_read)[0]

          if self.scaling_output:
            channel_data.append(myInt*scale_fac_uVolts_per_count)
          else:
            channel_data.append(myInt)

  
  """

  Clean Up (atexit)

  """
  def stop(self):
    print("Stopping streaming...\nWait for buffer to flush...")
    self.streaming = False
    self.char_write.write(b's')
    if self.log:
      logging.warning('sent <s>: stopped streaming')

  def disconnect(self):
    if(self.streaming == True):
      self.stop()
    print("Closing BLE..")
    self.char_discon.write(' ')
    # should not try to read/write anything after that, will crash
    self.gang.disconnect()
    logging.warning('BLE closed')
       

  """

      SETTINGS AND HELPERS

  """
  def warn(self, text):
    if self.log:
      #log how many packets where sent succesfully in between warnings
      if self.log_packet_count:
        logging.info('Data packets received:'+str(self.log_packet_count))
        self.log_packet_count = 0;
      logging.warning(text)
    print("Warning: %s" % text)

  def check_connection(self, interval = 2, max_packets_to_skip=10):
    # stop checking when we're no longer streaming
    if not self.streaming:
      return
    #check number of dropped packages and establish connection problem if too large
    if self.packets_dropped > max_packets_to_skip:
      #if error, attempt to reconect
      self.reconnect()
    # check again again in 2 seconds
    threading.Timer(interval, self.check_connection).start()

  def reconnect(self):
    self.packets_dropped = 0
    self.warn('Reconnecting')
    self.stop()
    time.sleep(0.5)
    self.ser.write(b'b')
    time.sleep(0.5)
    self.streaming = True
    #self.attempt_reconnect = False


class OpenBCISample(object):
  """Object encapulsating a single sample from the OpenBCI board."""
  def __init__(self, packet_id, channel_data, aux_data):
    self.id = packet_id;
    self.channel_data = channel_data;
    self.aux_data = aux_data;
