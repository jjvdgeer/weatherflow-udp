#!/usr/bin/env python
# Copyright 2017-2020 Arthur Emerson, vreihen@yahoo.com
# Distributed under the terms of the GNU Public License (GPLv3)

"""
This driver detects different sensors packets broadcast using the WeatherFlow
UDP JSON protocol, and it includes a mechanism to filter the incoming data
and map the filtered data onto the weewx database schema and identify the
type of data from each sensor.

Sensors are filtered based on a tuple that identifies uniquely each sensor.
A tuple consists of the observation name, a unique identifier for the hardware,
and the packet type, separated by periods:

  <observation_name>.<hardware_id>.<packet_type>

The filter and data types are specified in a sensor_map stanza in the driver
stanza.  For example, on an Air/Sky setup:

[WeatherFlowUDP]
    driver = user.weatherflowudp
    log_raw_packets = False
    udp_address = <broadcast>
    # udp_address = 0.0.0.0
    # udp_address = 255.255.255.255
    udp_port = 50222
    udp_timeout = 90
    share_socket = True

    [[sensor_map]]
        outTemp = air_temperature.AR-00004424.obs_air
        outHumidity = relative_humidity.AR-00004424.obs_air
        pressure =  station_pressure.AR-00004424.obs_air
        # lightning_strikes =  lightning_strike_count.AR-00004424.obs_air
        # avg_distance =  lightning_strike_avg_distance.AR-00004424.obs_air
        outTempBatteryStatus =  battery.AR-00004424.obs_air
        windSpeed = wind_speed.SK-00001234.rapid_wind
        windDir = wind_direction.SK-00001234.rapid_wind
        # lux = illuminance.SK-00001234.obs_sky
        UV = uv.SK-00001234.obs_sky
        rain = rain_accumulated.SK-00001234.obs_sky
        windBatteryStatus = battery.SK-00001234.obs_sky
        radiation = solar_radiation.SK-00001234.obs_sky
        # lightningYYY = distance.AR-00004424.evt_strike
        # lightningZZZ = energy.AR-00004424.evt_strike

*** If no sensor_map is specified, no data will be collected. ***

For a sample Tempest sensor_map, see the file sample_Tempest_sensor_map on GitHub:

https://github.com/captain-coredump/weatherflow-udp/blob/master/sample_Tempest_sensor_map

To identify sensors, run the driver directly. For help on the various options, try
  cd /home/weewx
  PYTHONPATH=/home/weewx/bin python ./bin/user/weatherflowudp.py --help

To identify the various observation_name options, start weewx
with this station driver installed and it will write the
entire matrix of available observation_names and sensor_types
to syslog or wherever weewx is configured to send log info.

Apologies for the long observation_names, but I figured that
it would be best if I used the documented field names from
WeatherFlow's UDP packet specs (v37 at the time of writing) 
with underscores between words so that the names were
consistent with their protocol documentation.  See
https://weatherflow.github.io/SmartWeather/api/udp.html

Options:

    log_raw_packets = False

    Enable writing all raw UDP packets received to syslog,
    or wherever weewx is configured to send log info.  Will
    fill up your logs pretty quickly, so only use it as
    a debugging tool or to identify sensors.

    udp_address = <broadcast>
    # udp_address = 0.0.0.0
    # udp_address = 255.255.255.255

    This is the broadcast address that we should be listening
    on for packets.  If the driver throws an error on start,
    try one of the other commented-out options (in order).
    This seems to be platform-specific.  All three work on
    Debian Linux and my Raspberry Pi, but only 0.0.0.0 works
    on my Macbook running OS-X or MacOS.  Don't ask about
    Windows, since I don't have a test platform to see
    if it will even work.

    udp_port = 50222

    The IP port that we should be listening for UDP packets
    from.  WeatherFlow's default is 50222.

    udp_timeout = 90

    The number of seconds that we should wait for an incoming
    packet on the UDP socket before we give up and log an
    error into syslog.  I cannot determine whether or not
    weewx cares whether a station driver is non-blocking or
    blocking, but encountered a situation in testing where
    the WeatherFlow Hub rebooted for a firmware update and
    it caused the driver to throw a timeout error and exit.
    I have no idea what the default timeout value even is, but
    decided to make it configurable in case it is important
    to someone else.  My default of 90 seconds seems reasonable,
    with the Air sending observations every 60 seconds.  If
    you are an old-school programmer like me who thinks that
    computers should wait forever until they receive data,
    the Python value "None" should disable the timeout.  In
    any case, the driver will just log an error into syslog
    and keep on processing.  It isn't like it is the end
    of the world if you pick a wrong value, but you may have
    a better chance of missing packets during the brief error
    trapping time with a really short duration.

    share_socket = False

    Whether or not the UDP socket should be shared with other
    local programs also listening for WeatherFlow packets.  Default
    is False because I suspect that some obscure Python implementation
    will have problems sharing the socket.  Feel free to set it to
    True if you have other apps running on your weewx host listening
    for WF UDP packets.

Finally, let me add a thank you to Matthew Wall for the
sensor map naming logic that I borrowed from his weewx-SDR
station driver code: 

https://github.com/matthewwall/weewx-sdr

I guess that I should also thank David St. John and the
"dream team" at WeatherFlow for all of the hard work and
forethought that they put into making this weather station
a reality.  I can't sing enough praises for whoever came
up with the idea to send observation packets out live via
UDP broadcasts, and think that they should be nominated
for a Nobel Prize or something...

"""

from __future__ import with_statement
import json
import time

import sys
from socket import *

import weewx.units
import weewx.drivers
import weewx.wxformulas
from weeutil.weeutil import tobool
import requests
from datetime import datetime
import calendar
from configobj import ConfigObj

# Default settings...
DRIVER_VERSION = "1.12"
HARDWARE_NAME = "WeatherFlow"
DRIVER_NAME = 'WeatherFlowUDP'

try:
    # Test for new-style weewx logging by trying to import weeutil.logger
    import weeutil.logger
    import logging

    log = logging.getLogger(__name__)


    def logdbg(msg):
        log.debug(msg)


    def loginf(msg):
        log.info(msg)


    def logwrn(msg):
        log.warn(msg)


    def logerr(msg):
        log.error(msg)

except ImportError:
    # Old-style weewx logging
    import syslog


    def logmsg(level, msg):
        syslog.syslog(level, 'weatherflowudp: %s:' % msg)


    def logdbg(msg):
        logmsg(syslog.LOG_DEBUG, msg)


    def loginf(msg):
        logmsg(syslog.LOG_INFO, msg)


    def logwrn(msg):
        logmsg(syslog.LOG_WARNING, msg)


    def logerr(msg):
        logmsg(syslog.LOG_ERR, msg)


# Observation record fields...
fields = dict()
fields['obs_air'] = ('time_epoch', 'station_pressure', 'air_temperature', 'relative_humidity', 'lightning_strike_count', 'lightning_strike_avg_distance', 'battery', 'report_interval')
fields['obs_sky'] = ('time_epoch', 'illuminance', 'uv', 'rain_accumulated', 'wind_lull', 'wind_avg', 'wind_gust', 'wind_direction', 'battery', 'report_interval', 'solar_radiation', 'local_day_rain_accumulation', 'precipitation_type', 'wind_sample_interval')
fields['rapid_wind'] = ('time_epoch', 'wind_speed', 'wind_direction')
fields['evt_precip'] = ('time_epoch',)
fields['evt_strike'] = ('time_epoch', 'distance', 'energy')
fields['obs_st'] = ('time_epoch', 'wind_lull', 'wind_avg', 'wind_gust', 'wind_direction', 'wind_sample_interval', 'station_pressure', 'air_temperature', 'relative_humidity', 'illuminance', 'uv', 'solar_radiation', 'rain_accumulated', 'precipitation_type', 'lightning_strike_avg_distance', 'lightning_strike_count', 'battery', 'report_interval')

def loader(config_dict, engine):
    return WeatherFlowUDPDriver(**config_dict[DRIVER_NAME])


def sendMyLoopPacket(pkt,sensor_map, add_interval):
    packet = dict()
    if 'time_epoch' in pkt:
        packet = {
            'dateTime': pkt['time_epoch'],
            'usUnits' : weewx.METRICWX
        }
    if add_interval:
        packet.update({'interval':1})

    for pkt_weewx, pkt_label in sensor_map.items():
        if pkt_label.replace("-","_") in pkt:
           packet[pkt_weewx] = pkt[pkt_label.replace("-","_")]

    return packet

def parseUDPPacket(pkt):
    packet = dict()
    if 'serial_number' in pkt:
        if 'type' in pkt:
            serial_number = pkt['serial_number'].replace("-","_")
            pkt_label = serial_number + "." + pkt['type']
            for key in pkt:
                packet[key + "." + pkt_label] = pkt[key]

            if pkt['type'] in ('obs_air', 'obs_sky', 'obs_st'):
                packet['time_epoch'] = pkt['obs'][0][0]
                for key, value in zip(fields[pkt['type']], pkt['obs'][0]):
                    packet[key + "." + pkt_label] = value

            elif pkt['type'] == 'rapid_wind':
                packet['time_epoch'] = pkt['ob'][0]
                for key, value in zip(fields['rapid_wind'], pkt['ob']):
                    packet[key + "." + pkt_label] = value

            elif pkt['type'] in ('evt_strike', 'evt_precip'):
                packet['time_epoch'] = pkt['evt'][0]
                for key, value in zip(fields[pkt['type']], pkt['evt']):
                    packet[key + "." + pkt_label] = value

            elif pkt['type'] == 'device_status':
                packet['time_epoch'] = pkt['timestamp']

            elif pkt['type'] == 'hub_status':
                packet['time_epoch'] = pkt['timestamp']

            elif pkt['type'][0:2] == 'X_':
                packet['time_epoch'] = int(time.time())

            elif pkt['type'] not in ('light_debug') :
                logerr("Unknown packet type: '%s'" % pkt['type'])

        else:
            loginf('Corrupt UDP packet? %s' % pkt)
    else:
        loginf('Corrupt UDP packet? %s' % pkt)
    return packet

def getStationsUrl(token):
    return 'https://swd.weatherflow.com/swd/rest/stations?token={token}'.format(token = token)

def getObservationsUrl(start, end, token, device_id):
    return 'https://swd.weatherflow.com/swd/rest/observations/device/{device_id}?token={token}&time_start={start}&time_end={end}'.format(token = token, device_id = device_id, start = start, end = end)

def getStationDevices(token):
    response = requests.get(getStationsUrl(token))
    if (response.status_code != 200):
        raise Exception("Could not fetch station information from WeatherFlow webservice: {}".format(response))
    stations = response.json()["stations"]
    device_id_dict = dict()
    device_dict = dict()
    for station in stations:
        for device in station["devices"]:
            if 'serial_number' in device:
                device_id_dict.update({device["device_id"]:device["serial_number"]})
                device_dict.update({device["serial_number"]:device["device_id"]})
    return device_id_dict, device_dict

def readDataFromWF(start, token, devices, device_dict, batch_size):
    isFinished = False
    while not isFinished: # end > calendar.timegm(datetime.utcnow().utctimetuple()):
        end = start + batch_size - 1
        lastTimestamp = None
        logdbg('Reading from {} to {}'.format(datetime.utcfromtimestamp(start), datetime.utcfromtimestamp(end)))
        results = list()
        timestamps = None
        for device in devices:
            logdbg('Reading for {} from {} to {}'.format(device, datetime.utcfromtimestamp(start), datetime.utcfromtimestamp(lastTimestamp or end)))
            response = requests.get(getObservationsUrl(start, lastTimestamp or end, token, device_dict[device]))
            if (response.status_code != 200):
                raise Exception("Could not fetch records from WeatherFlow webservice: {}".format(response))
            jsonResponse = response.json()
            if lastTimestamp == None and jsonResponse['obs'] != None:
                lastTimestamp = sorted(jsonResponse['obs'], key = lambda i: i[0], reverse = True)[0][0]

            result = dict()
            result['device_id'] = jsonResponse['device_id']
            result['type'] = jsonResponse['type']
            observations = sorted((jsonResponse['obs'] or list()), key = lambda x : x[0])
            newTimestamps = [observation[0] for observation in observations]
            timestamps = timestamps or (timestamps or list()) + list(set(newTimestamps) - set(timestamps or list()))
            result['obs'] = dict(zip(newTimestamps, observations))

            results.append(result)
        combinedResult = dict()
        combinedResult['device_ids'] = [result['device_id'] for result in results]
        combinedResult['types'] = [result['type'] for result in results]
        combinedResult['obs'] = list()
        for timestamp in sorted(timestamps):
            observationsForTimestamp = list()
            for result in results:
                if 'obs' in result and timestamp in result['obs']:
                    observationsForTimestamp.append(result['obs'][timestamp])
                else:
                    observationsForTimestamp.append(None)
            combinedResult['obs'].append(observationsForTimestamp)

        yield combinedResult
        if end > calendar.timegm(datetime.utcnow().utctimetuple()):
            isFinished = True
        else:
            start = end if lastTimestamp == None else lastTimestamp + 1

def parseRestPacket(pkt, device_id_dict):
    label_list = list()
    pos = 0
    for device_id in pkt['device_ids']:        
        label_list.append(device_id_dict[device_id].replace("-","_") + "." + pkt['types'][pos])
        pos += 1
    fields_list = list()
    for type in pkt['types']:
        fields_list.append(fields[type])
    for observations in pkt['obs']:
        packet = dict()
        pos = 0
        for observation in observations:
            if observation == None:
                continue
            packet['time_epoch'] = observation[0]
            for key, value in zip(fields_list[pos], observation):
                packet[key + "." + label_list[pos]] = value
            pos += 1
        yield packet

def getDevices(devicesList, devices):
    devicesList = ensureList(devicesList)
    result = list()
    for device in devicesList:
        if device != '':
            if device in devices:
                result.append(device.strip().upper())
            else:
                logwrn('Configured device {} is unknown. Skipped.'.format(device))
    if not result:
        raise Exception("None of the configured devices ({}) were available for the given API-token. Aborting.".format(', '.join(devicesList)))
    return result

def ensureList(inputList):
    try:
        basestring
    except NameError:
        basestring = str
    if isinstance(inputList, basestring):
        result = list()
        result.append(inputList)
        return result
    else:
        return inputList

def getSensorMap(devices, device_id_dict):
    configObj = ConfigObj()
    configObj['sensor_map'] = {}
    devices.reverse()
    for device in devices:
        if device not in device_id_dict.values():
            logwrn('Unknown device {}, skipping'.format(device))
            continue
        typeString = device[0:3]
        packageTypes = {
            'ST-':'obs_st',
            'AR-':'obs_air',
            'SK-':'obs_sky',
            'HB-':None
        }
        fieldsDictionary = {
            'ST-':
                {
                    'outTemp': 'air_temperature',
                    'outHumidity': 'relative_humidity',
                    'pressure': 'station_pressure',
                    'lightning_strike_count': 'lightning_strike_count',
                    'lightning_distance': 'lightning_strike_avg_distance',
                    'outTempBatteryStatus': 'battery',
                    'windSpeed': 'wind_avg',
                    'windDir': 'wind_direction',
                    'windGust': 'wind_gust',
                    'luminosity': 'illuminance',
                    'UV': 'uv',
                    'rain': 'rain_accumulated',
                    'windBatteryStatus': 'battery',
                    'radiation': 'solar_radiation'
                },
            'AR-':
                {
                    'outTemp': 'air_temperature',
                    'outHumidity': 'relative_humidity',
                    'pressure': 'station_pressure',
                    'lightning_strike_count': 'lightning_strike_count',
                    'lightning_distance': 'lightning_strike_avg_distance',
                    'outTempBatteryStatus': 'battery'
                },
            'SK-':
                {
                    'windSpeed': 'wind_avg',
                    'windDir': 'wind_direction',
                    'windGust': 'wind_gust',
                    'luminosity': 'illuminance',
                    'UV': 'uv',
                    'rain': 'rain_accumulated',
                    'windBatteryStatus': 'battery',
                    'radiation': 'solar_radiation'
                },
            'HB-': { }
        }
        if typeString not in fieldsDictionary:
            logwrn('Unknown type for device {}' % device)
            continue
        fields = fieldsDictionary[typeString]
        errors = False
        for field in fields:
            if field in configObj['sensor_map'].dict():
                errors = True
                logwrn('Cannot map field {} to {} as it is already set to \'{}\''.format(field, device, configObj['sensor_map'][field]))
            configObj['sensor_map'].update({field: '{}.{}.{}'.format(fields[field], device, packageTypes[typeString])})
        if errors:
            logwrn('Mapping errors occurred. You should probably configure a manual sensor-map')
    return configObj['sensor_map']

class WeatherFlowUDPDriver(weewx.drivers.AbstractDevice):

    def __init__(self, **stn_dict):
        loginf('driver version is %s' % DRIVER_VERSION)
        self._log_raw_packets = tobool(stn_dict.get('log_raw_packets', False))
        self._udp_address = stn_dict.get('udp_address', '<broadcast>')
        self._udp_port = int(stn_dict.get('udp_port', 50222))
        self._udp_timeout = int(stn_dict.get('udp_timeout', 90))
        self._share_socket = tobool(stn_dict.get('share_socket', False))
        self._sensor_map = stn_dict.get('sensor_map', None)
        self._token = stn_dict.get('token', '')
        self._batch_size = int(stn_dict.get('batch_size', 24 * 60 * 60))
        self._device_id_dict, self._device_dict = getStationDevices(self._token)
        self._devices = getDevices(stn_dict.get('devices', list(self._device_dict.keys())), self._device_dict.keys())
        if self._sensor_map == None:
            self._sensor_map = getSensorMap(self._devices, self._device_id_dict)

        loginf('sensor map is %s' % self._sensor_map)
        loginf('*** Sensor names per packet type')

        for pkt_type in fields:
            loginf('packet %s: %s' % (pkt_type,fields[pkt_type]))

    def hardware_name(self):
        return HARDWARE_NAME

    def genLoopPackets(self):
        for udp_packet in self.gen_udp_packets():
            m2 = parseUDPPacket(udp_packet)
            m3 = sendMyLoopPacket(m2, self._sensor_map, False)
            if len(m3) > 2:
                loginf('Import from UDP: %s' % datetime.utcfromtimestamp(m3['dateTime']))
                yield m3

    def gen_udp_packets(self):
        """Yield raw UDP packets"""
        loginf('Listening for UDP broadcasts to IP address %s on port %s, with timeout %s and share_socket %s...'
               % (self._udp_address,self._udp_port,self._udp_timeout,self._share_socket))

        sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        try:
            if self._share_socket:
                sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
            sock.bind((self._udp_address,self._udp_port))
            sock.settimeout(self._udp_timeout)

            while True:
                try:
                    m0, host_info = sock.recvfrom(1024)
                except timeout:
                    logerr('Socket timeout waiting for incoming UDP packet!')
                else:
                    # Decode the JSON. Some base stations have emitted datagrams that are not pure UTF-8, so
                    # be prepared to catch the exception.
                    try:
                        m1 = json.loads(m0)
                    except UnicodeDecodeError:
                        loginf("Unable to decode packet %s" % m0)
                    else:
                        if self._log_raw_packets:
                            loginf('raw packet: %s' % m1)
                        yield m1
        finally:
            sock.close()

    @property
    def archive_interval(self):
        return 1

    def genArchiveRecords(self, since_ts):
        if since_ts == None:
            since_ts = int(time.time()) - 365 * 24 * 60 * 60

        loginf('Reading from {}'.format(datetime.utcfromtimestamp(since_ts)))
        if self._token != "":
            for packet in readDataFromWF(since_ts + 1, self._token, self._devices, self._device_dict, self._batch_size):
                for observation in parseRestPacket(packet, self._device_id_dict):
                    m3 = sendMyLoopPacket(observation, self._sensor_map, True)
                    if len(m3) > 3:
                        loginf('import from REST %s' % datetime.utcfromtimestamp(m3['dateTime']))
                        yield m3

if __name__ == '__main__':
    import optparse
    import weeutil.logger
    from weeutil.weeutil import to_sorted_string

    weewx.debug = 2

    weeutil.logger.setup('weatherflow', {})

    usage = """Usage: python -m weatherflow --help
       python -m weatherflow --version
       python -m weatherflow [--host=HOST] [--port=PORT] [--timeout=TIMEOUT] [--share-socket]
                             [--hide-raw] [--hide-parsed]"""

    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--version', action='store_true',
                      help='Display driver version')
    parser.add_option('--address', default='255.255.255.255',
                      help='UDP address to use. Default is "255.255.255.255".',
                      metavar="ADDR")
    parser.add_option('--port', type="int", default=50222,
                      help='Socket port to use. Default is "50222"',
                      metavar="PORT")
    parser.add_option('--timeout', type="int", default=20,
                      help="How long to wait for a packet.")
    parser.add_option('--share-socket', default=False, action="store_true",
                      help="Allow another process to access the port.")
    parser.add_option('--hide-raw', default=False, action='store_true',
                      help="Do not show raw UDP packets.")
    parser.add_option('--hide-parsed', default=False, action='store_true',
                      help="Do not show parsed UDP packets.")
    (options, args) = parser.parse_args()

    if options.version:
        print("Weatherflow driver version %s" % DRIVER_VERSION)
        exit(0)

    print("Using address '%s' on port %d" % (options.address, options.port))

    config_dict = {
        'WeatherFlowUDP': {
            'address': options.address,
            'port': options.port,
            'timeout': options.timeout,
            'share_socket': options.share_socket,
        }
    }

    device = loader(config_dict, None)

    for pkt in device.gen_udp_packets():
        if not options.hide_raw:
            print('raw:', pkt)
        parsed = parseUDPPacket(pkt)
        if not options.hide_parsed:
            print('parsed:', to_sorted_string(parsed))
