#!/usr/bin/python3

import minimalmodbus
import yaml
import daemon
import logging
from logging.handlers import RotatingFileHandler
import argparse
from time import sleep
from datetime import datetime
from threading import Lock
from mqtt_discovery import DiscoverMsgSensor, DiscoverMsgNumber, DiscoverMsgSwitch
from inverter import Inverter
from mqtt import Mqtt
from config import Config

VERSION = "0.7"

class Solis2Mqtt:
    def __init__(self):    
        self.cfg = Config('config.yaml')
        self.register_cfg = ...
        self.load_register_cfg()
        self.inverter = Inverter(self.cfg['device'], self.cfg['slave_address'])
        self.inverter_lock = Lock()
        self.inverter_offline = False
        self.mqtt = Mqtt(self.cfg['inverter']['name'], self.cfg['mqtt'])
        self.last_clock_update = None

    def load_register_cfg(self, register_data_file='solis_modbus.yaml'):
        with open(register_data_file) as smfile:
            self.register_cfg = yaml.load(smfile, yaml.Loader)

    def generate_ha_discovery_topics(self):
        for entry in self.register_cfg:
            if entry['active'] and 'homeassistant' in entry:
                if entry['homeassistant']['device'] == 'sensor':
                    logging.info("Generating discovery topic for sensor: "+entry['name'])
                    self.mqtt.publish(f"homeassistant/sensor/{self.cfg['inverter']['name']}/{entry['name']}/config",
                                      str(DiscoverMsgSensor(entry['description'],
                                                            entry['name'],
                                                            entry['unit'],
                                                            entry['homeassistant']['device_class'],
                                                            entry['homeassistant']['state_class'],
                                                            self.cfg['inverter']['name'],
                                                            self.cfg['inverter']['model'],
                                                            self.cfg['inverter']['manufacturer'],
                                                            VERSION)),
                                      retain=True)
                elif entry['homeassistant']['device'] == 'number':
                    logging.info("Generating discovery topic for number: " + entry['name'])
                    self.mqtt.publish(f"homeassistant/number/{self.cfg['inverter']['name']}/{entry['name']}/config",
                                      str(DiscoverMsgNumber(entry['description'],
                                                            entry['name'],
                                                            entry['homeassistant']['min'],
                                                            entry['homeassistant']['max'],
                                                            entry['homeassistant']['step'],
                                                            self.cfg['inverter']['name'],
                                                            self.cfg['inverter']['model'],
                                                            self.cfg['inverter']['manufacturer'],
                                                            VERSION)),
                                      retain=True)
                elif entry['homeassistant']['device'] == "switch":
                    logging.info("Generating discovery topic for switch: " + entry['name'])
                    self.mqtt.publish(f"homeassistant/switch/{self.cfg['inverter']['name']}/{entry['name']}/config",
                                      str(DiscoverMsgSwitch(entry['description'],
                                                            entry['name'],
                                                            entry['homeassistant']['payload_on'],
                                                            entry['homeassistant']['payload_off'],
                                                            self.cfg['inverter']['name'],
                                                            self.cfg['inverter']['model'],
                                                            self.cfg['inverter']['manufacturer'],
                                                            VERSION)),
                                      retain=True)
                else:
                    logging.error("Unknown homeassistant device type: "+entry['homeassistant']['device'])

    def update_clock(self):
        clock_register_str = self.cfg["inverter"].get("clock_register")
        if clock_register_str is None:
            logging.debug("No clock register config")
            return

        clock_register = int(clock_register_str)

        # Only perform clock checks updates hourly
        if self.last_clock_update and (datetime.now() - self.last_clock_update).total_seconds() < 60*60:
            logging.debug("Recent clock update")
            return

        logging.debug(f"Reading clock registers from {clock_register}")
        inverter_clock_values = self.inverter.read_registers(registeraddress=clock_register, number_of_registers=6, functioncode=3)

        if inverter_clock_values[0] < 10 or inverter_clock_values[0] > 100 or inverter_clock_values[1] > 12 or inverter_clock_values[2] > 31:
            logging.error(f"Inverter clock values seem incorrect. Skipping. {inverter_clock_values}")
            return

        # The inverter stores the year in a two digit form
        inverter_clock_values[0] = inverter_clock_values[0] + 2000

        inverter_clock = datetime(*inverter_clock_values)

        now = datetime.now()
        delta_seconds = (now - inverter_clock).total_seconds()

        if abs(delta_seconds) < 5:
            logging.info(f"Inverter clock is accurate (delta={delta_seconds}s)")
            return

        logging.info(f"Inverter clock is out by {delta_seconds}s. Updating....  (now={now} inverter={inverter_clock})")

        correct_values = [now.year-2000, now.month, now.day, now.hour, now.minute, now.second]
        for offset in range(6):
            self.inverter.write_register(
                registeraddress=clock_register+offset, 
                value=correct_values[offset],
                functioncode=6)

        inverter_clock_values = self.inverter.read_registers(registeraddress=clock_register, number_of_registers=6, functioncode=3)
        logging.info(f"New inverter clock register values: {inverter_clock_values}")
        self.last_clock_update = now


    def subscribe(self):
        for entry in self.register_cfg:
            if 'write_function_code' in entry['modbus']:
                if not self.mqtt.on_message:
                    self.mqtt.on_message = self.on_mqtt_message
                logging.info("Subscribing to: "+self.cfg['inverter']['name'] + "/" + entry['name'] + "/set")
                self.mqtt.persistent_subscribe(self.cfg['inverter']['name'] + "/" + entry['name'] + "/set")

    def read_composed_date(self, register, functioncode):
        year = self.inverter.read_register(register[0], functioncode=functioncode)
        month = self.inverter.read_register(register[1], functioncode=functioncode)
        day = self.inverter.read_register(register[2], functioncode=functioncode)
        hour = self.inverter.read_register(register[3], functioncode=functioncode)
        minute = self.inverter.read_register(register[4], functioncode=functioncode)
        second = self.inverter.read_register(register[5], functioncode=functioncode)
        return f"20{year:02d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"

    def on_mqtt_message(self, client, userdata, msg):
        for el in self.register_cfg:
            if el['name'] == msg.topic.split('/')[-2]:
                register_cfg = el['modbus']
                break

        str_value = msg.payload.decode('utf-8')
        if 'number_of_decimals' in register_cfg and register_cfg['number_of_decimals'] > 0:
            value = float(str_value)
        else:
            value = int(str_value)
        with self.inverter_lock:
            try:
                self.inverter.write_register(register_cfg['register'],
                                             value,
                                             register_cfg['number_of_decimals'],
                                             register_cfg['write_function_code'],
                                             register_cfg['signed'])
            except (minimalmodbus.NoResponseError, minimalmodbus.InvalidResponseError):
                if not self.inverter_offline:
                    logging.exception(f"Error while writing message to inverter. Topic: '{msg.topic}, "
                                      f"Value: '{str_value}', Register: '{register_cfg['register']}'.")

    def main(self):
        self.generate_ha_discovery_topics()
        self.subscribe()
        while True:
            logging.debug("Inverter scan start at " + datetime.now().isoformat())

            self.update_clock()

            for entry in self.register_cfg:
                if not entry['active'] or 'function_code' not in entry['modbus'] :
                    continue

                try:
                    if entry['modbus']['read_type'] == "register":
                        with self.inverter_lock:
                            value = self.inverter.read_register(entry['modbus']['register'],
                                                                number_of_decimals=entry['modbus'][
                                                                    'number_of_decimals'],
                                                                functioncode=entry['modbus']['function_code'],
                                                                signed=entry['modbus']['signed'])

                    elif entry['modbus']['read_type'] == "long":
                        with self.inverter_lock:
                            value = self.inverter.read_long(entry['modbus']['register'],
                                                            functioncode=entry['modbus']['function_code'],
                                                            signed=entry['modbus']['signed'])
                    elif entry['modbus']['read_type'] == "composed_datetime":
                        with self.inverter_lock:
                            value = self.read_composed_date(entry['modbus']['register'],
                                                            functioncode=entry['modbus']['function_code'])
                # NoResponseError occurs if inverter is off,
                # InvalidResponseError might happen when inverter is starting up or shutting down during a request
                except (minimalmodbus.NoResponseError, minimalmodbus.InvalidResponseError):
                    # in case we didn't have a exception before
                    if not self.inverter_offline:
                        logging.info("Inverter not reachable")
                        self.inverter_offline = True

                    if 'homeassistant' in entry and entry['homeassistant']['state_class'] == "measurement":
                        value = 0
                    else:
                        continue
                else:
                    self.inverter_offline = False
                    logging.info(f"Read {entry['description']} - {value}{entry['unit'] if entry['unit'] else ''}")

                self.mqtt.publish(f"{self.cfg['inverter']['name']}/{entry['name']}", value, retain=True)

            # wait with next poll configured interval, or if inverter is not responding ten times the interval
            sleep_duration = self.cfg['poll_interval'] if not self.inverter_offline else self.cfg['poll_interval_if_off']
            logging.debug(f"Inverter scanning paused for {sleep_duration} seconds")
            sleep(sleep_duration)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Solis inverter to mqtt bridge.')
    parser.add_argument('-d', '--daemon', action='store_true', help='start as daemon')
    parser.add_argument('-v', '--verbose', action='store_true', help="verbose logging")
    args = parser.parse_args()

    def start_up(is_daemon, verbose):
        log_level = logging.DEBUG if verbose else logging.INFO
        handler = RotatingFileHandler("solis2mqtt.log", maxBytes=1024 * 1024 * 10,
                                      backupCount=1) if is_daemon else logging.StreamHandler()
        logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(message)s", handlers=[handler])
        logging.info("Starting up...")
        Solis2Mqtt().main()

    if args.daemon:
        with daemon.DaemonContext(working_directory='./'):
            try:
                start_up(args.daemon, args.verbose)
            except:
                logging.exception("Unhandled exception:")
    else:
        start_up(args.daemon, args.verbose)