#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""Evohome RF - a RAMSES-II protocol decoder & analyser.

Construct a command (packet that is to be sent).
"""

import asyncio
from datetime import datetime as dt, timedelta as td
from functools import total_ordering
import json
import logging
import struct
from types import SimpleNamespace
from typing import Optional
import zlib

from .const import (
    _dev_mode_,
    CODES_SANS_DOMAIN_ID,
    CODE_SCHEMA,
    COMMAND_FORMAT,
    COMMAND_REGEX,
    HGI_DEVICE,
    SYSTEM_MODE_LOOKUP,
    ZONE_MODE_LOOKUP,
    ZONE_MODE_MAP,
)
from .exceptions import ExpiredCallbackError
from .helpers import dt_now, dtm_to_hex, extract_addrs, str_to_hex, temp_to_hex

DAY_OF_WEEK = "day_of_week"
HEAT_SETPOINT = "heat_setpoint"
SWITCHPOINTS = "switchpoints"
TIME_OF_DAY = "time_of_day"

SCHEDULE = "schedule"
ZONE_IDX = "zone_idx"

TIMER_SHORT_SLEEP = 0.05
TIMER_LONG_TIMEOUT = td(seconds=60)

FIVE_MINS = td(minutes=5)


Priority = SimpleNamespace(LOWEST=8, LOW=6, DEFAULT=4, HIGH=2, HIGHEST=0)

DEV_MODE = _dev_mode_

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


def _pkt_header(pkt: str, rx_header=None) -> Optional[str]:
    """Return the QoS header of a packet."""

    verb = pkt[4:6]
    if rx_header:
        verb = "RP" if verb == "RQ" else " I"  # RQ/RP, or W/I
    code = pkt[41:45]
    src, dst, _ = extract_addrs(pkt)
    addr = dst.id if src.type == "18" else src.id
    payload = pkt[50:]

    header = "|".join((verb, addr, code))

    if code in ("0001", "7FFF") and rx_header:
        return

    if code in ("0005", "000C"):  # zone_idx, device_class
        return "|".join((header, payload[:4]))

    if code == "0404":  # zone_schedule: zone_idx, frag_idx
        return "|".join((header, payload[:2] + payload[10:12]))

    if code == "0418":  # fault_log: log_idx
        if payload == CODE_SCHEMA["0418"]["null_rp"]:
            return header
        return "|".join((header, payload[4:6]))

    if code in CODES_SANS_DOMAIN_ID:  # have no domain_id
        return header

    return "|".join((header, payload[:2]))  # assume has a domain_id


@total_ordering
class Command:
    """The command class."""

    def __init__(self, verb, dest_addr, code, payload, **kwargs) -> None:
        """Initialise the class."""

        # self._loop = ...

        self.verb = verb
        self.from_addr = kwargs.get("from_addr", HGI_DEVICE.id)
        self.dest_addr = dest_addr if dest_addr is not None else self.from_addr
        self.code = code
        self.payload = payload

        self._is_valid = None
        if not self.is_valid:
            raise ValueError(f"Invalid parameter values for command: {self}")

        self.callback = kwargs.get("callback", {})  # TODO: use voluptuous
        if self.callback:
            self.callback["args"] = self.callback.get("args", [])
            self.callback["kwargs"] = self.callback.get("kwargs", {})

        self.qos = self._qos
        self.qos.update(kwargs.get("qos", {}))
        self._priority = self.qos["priority"]
        self._priority_dtm = dt_now()  # used for __lt__, etc.

        self._rx_header = None
        self._tx_header = None

    def __str__(self) -> str:
        """Return a brief readable string representation of this object."""

        return COMMAND_FORMAT.format(
            self.verb,
            self.from_addr,
            self.dest_addr,
            self.code,
            int(len(self.payload) / 2),
            self.payload,
        )

    @property
    def _qos(self) -> dict:
        """Return the QoS params of this (request) packet."""

        # the defaults for these are in packet.py
        # qos = {"priority": Priority.DEFAULT, "retries": 3, "timeout": td(seconds=0.5)}
        qos = {"priority": Priority.DEFAULT, "retries": 3}

        if self.code in ("0016", "1F09") and self.verb == "RQ":
            qos.update({"priority": Priority.HIGH, "retries": 5})

        elif self.code == "0404" and self.verb in ("RQ", " W"):
            qos.update({"priority": Priority.HIGH, "timeout": td(seconds=0.30)})

        elif self.code == "0418" and self.verb == "RQ":
            qos.update({"priority": Priority.LOW, "retries": 3})

        return qos

    @property
    def tx_header(self) -> str:
        """Return the QoS header of this (request) packet."""
        if self._tx_header is None:
            self._tx_header = _pkt_header(f"... {self}")
        return self._tx_header

    @property
    def rx_header(self) -> Optional[str]:
        """Return the QoS header of a response packet (if any)."""
        if self.tx_header and self._rx_header is None:
            self._rx_header = _pkt_header(f"... {self}", rx_header=True)
        return self._rx_header

    # @property
    # def null_header(self) -> Optional[str]:
    #     """Return the QoS header of a null response packet (if any)."""
    #     if self.tx_header and self._rx_header is None:
    #         self._rx_header = _pkt_header(f"... {self}", null_header=True)
    #     return self._rx_header

    @property
    def is_valid(self) -> Optional[bool]:
        """Return True if a valid command, otherwise return False/None."""

        if self._is_valid is not None:
            return self._is_valid

        if not COMMAND_REGEX.match(str(self)):
            self._is_valid = False
        elif 0 > len(self.payload) > 96:
            self._is_valid = False
        else:
            self._is_valid = True

        if not self._is_valid:
            _LOGGER.debug("Command has an invalid structure: %s", self)

        return self._is_valid

    @staticmethod
    def _is_valid_operand(other) -> bool:
        return hasattr(other, "_priority") and hasattr(other, "_priority_dtm")

    def __eq__(self, other) -> bool:
        if not self._is_valid_operand(other):
            return NotImplemented
        return (self._priority, self._priority_dtm) == (
            other._priority,
            other._priority_dtm,
        )

    def __lt__(self, other) -> bool:
        if not self._is_valid_operand(other):
            return NotImplemented
        return (self._priority, self._priority_dtm) < (
            other._priority,
            other._priority_dtm,
        )

    @classmethod  # constructor for 10A0  # TODO
    def dhw_params(
        cls,
        ctl_id,
        domain_id,
        setpoint: float = 50,
        overrun: int = 5,
        differential: float = 1.0,
    ):
        """Constructor to set the params of the DHW (c.f. parser_10a0)."""

        payload = f"{domain_id:02X}" if isinstance(domain_id, int) else domain_id

        assert setpoint is None or 30 <= setpoint <= 85, setpoint
        assert overrun is None or 0 <= overrun <= 10, overrun
        assert differential is None or 1 <= differential <= 10, differential

        payload += temp_to_hex(setpoint)
        payload += f"{overrun:02X}"
        payload += temp_to_hex(differential)

        return cls(" W", ctl_id, "10A0", payload)

    @classmethod  # constructor for 1F41  # TODO
    def dhw_mode(cls, ctl_id, domain_id, active: bool, mode, until=None):
        """Constructor to set/reset the mode of the DHW (c.f. parser_1f41)."""

        payload = f"{domain_id:02X}" if isinstance(domain_id, int) else domain_id

        assert isinstance(active, bool), active
        assert mode in ZONE_MODE_LOOKUP, mode

        payload += f"{int(active):02X}"
        payload += f"{ZONE_MODE_LOOKUP[mode]}FFFFFF"
        if ZONE_MODE_LOOKUP[mode] == "04":
            payload += dtm_to_hex(until)

        return cls(" W", ctl_id, "1F41", payload)

    @classmethod  # constructor for 1030  # TODO
    def mix_valve_params(
        cls,
        ctl_id,
        zone_idx,
        max_flow_setpoint=55,
        min_flow_setpoint=15,
        valve_run_time=150,
        pump_run_time=15,
    ):
        """Constructor to set the mix valve params of a zone (c.f. parser_1030)."""

        payload = f"{zone_idx:02X}" if isinstance(zone_idx, int) else zone_idx

        assert 0 <= max_flow_setpoint <= 99, max_flow_setpoint
        assert 0 <= min_flow_setpoint <= 50, min_flow_setpoint
        assert 0 <= valve_run_time <= 240, valve_run_time
        assert 0 <= pump_run_time <= 99, pump_run_time

        payload += f"C801{max_flow_setpoint:02X}"
        payload += f"C901{min_flow_setpoint:02X}"
        payload += f"CA01{valve_run_time:02X}"
        payload += f"CB01{pump_run_time:02X}"
        payload += f"CC01{1:02X}"

        return cls(" W", ctl_id, "1030", payload)

    @classmethod  # constructor for 2E04  # TODO
    def system_mode(cls, ctl_id, mode=None, until=None):
        """Constructor to set/reset the mode of a system (c.f. parser_2e04)."""

        payload = ""

        assert mode in SYSTEM_MODE_LOOKUP, mode

        payload += f"{SYSTEM_MODE_LOOKUP[mode]}FFFFFF"
        if SYSTEM_MODE_LOOKUP[mode] == "04":
            payload += dtm_to_hex(until)

        return cls(" W", ctl_id, "2E04", payload)

    @classmethod  # constructor for 313F
    def system_time(cls, ctl_id, datetime):
        """Constructor to set the datetime of a system (c.f. parser_313f)."""
        #  W --- 30:185469 01:037519 --:------ 313F 009 0060003A0C1B0107E5

        return cls(" W", ctl_id, "313F", f"006000{dtm_to_hex(datetime)}")

    @classmethod  # constructor for 1100  # TODO
    def tpi_params(
        cls,
        ctl_id,
        domain_id,
        cycle_rate=3,  # TODO: check
        min_on_time=5,  # TODO: check
        min_off_time=5,  # TODO: check
        proportional_band_width=None,  # TODO: check
    ):
        """Constructor to set the TPI params of a system (c.f. parser_1100)."""

        payload = f"{domain_id:02X}" if isinstance(domain_id, int) else domain_id

        assert cycle_rate is None or cycle_rate in (3, 6, 9, 12), cycle_rate
        assert min_on_time is None or 1 <= min_on_time <= 5, min_on_time
        assert min_off_time is None or 1 <= min_off_time <= 5, min_off_time
        assert (
            proportional_band_width is None or 1.5 <= proportional_band_width <= 3.0
        ), proportional_band_width

        payload += f"{cycle_rate * 4:02X}"
        payload += f"{int(min_on_time * 4):02X}"
        payload += f"{int(min_off_time * 4):02X}FF"
        payload += f"{temp_to_hex(proportional_band_width)}01"

        return cls(" W", ctl_id, "1100", payload)

    @classmethod  # constructor for 000A  # TODO
    def zone_config(
        cls,
        ctl_id,
        zone_idx,
        min_temp: int = 5,
        max_temp: int = 35,
        local_override: bool = False,
        openwindow_function: bool = False,
        multiroom_mode: bool = False,
    ):
        """Constructor to set the config of a zone (c.f. parser_000a)."""

        payload = f"{zone_idx:02X}" if isinstance(zone_idx, int) else zone_idx

        assert 5 <= min_temp <= 30, min_temp
        assert 0 <= max_temp <= 35, max_temp
        assert isinstance(local_override, bool), local_override
        assert isinstance(openwindow_function, bool), openwindow_function
        assert isinstance(multiroom_mode, bool), multiroom_mode

        bitmap = 0 if local_override else 1
        bitmap |= 0 if openwindow_function else 2
        bitmap |= 0 if multiroom_mode else 16

        payload += f"{bitmap}"
        payload += temp_to_hex(min_temp)
        payload += temp_to_hex(max_temp)

        return cls(" W", ctl_id, "000A", payload)

    @classmethod  # constructor for 2349
    def zone_mode(cls, ctl_id, zone_idx, mode=None, setpoint=None, until=None):
        """Constructor to set/reset the mode of a zone (c.f. parser_2349).

        The setpoint has a resolution of 0.1 C. If a setpoint temperature is required,
        but none is provided, evohome will use the maximum possible value.

        The until has a resolution of 1 min.

        Incompatible combinations:
          - mode == Follow & setpoint not None (will silently ignore setpoint)
          - mode == Temporary & until is None (will silently ignore)
        """
        #  W --- 18:013393 01:145038 --:------ 2349 013 0004E201FFFFFF330B1A0607E4
        #  W --- 22:017139 01:140959 --:------ 2349 007 0801F400FFFFFF

        payload = f"{zone_idx:02X}" if isinstance(zone_idx, int) else zone_idx

        assert mode in ZONE_MODE_LOOKUP, mode

        if mode is not None:
            if isinstance(mode, int):
                mode = f"{mode:02X}"
            elif not isinstance(mode, str):
                raise TypeError(f"Invalid zone mode: {mode}")
            if mode in ZONE_MODE_MAP:
                mode = ZONE_MODE_MAP["mode"]
            elif mode not in ZONE_MODE_LOOKUP:
                raise TypeError(f"Unknown zone mode: {mode}")

        elif until is None:  # mode is None
            mode = "advanced_override" if setpoint else "follow_schedule"
        else:  # if until is not None:
            mode = "temporary_override" if setpoint else "advanced_override"
        if until is None:
            mode = "advanced_override" if mode == "temporary_override" else mode

        payload += temp_to_hex(setpoint)  # None means max, if a temp is required
        payload += ZONE_MODE_LOOKUP[mode] + "FFFFFF"
        payload += "" if until is None else dtm_to_hex(until)

        return cls(" W", ctl_id, "2349", payload)

    @classmethod  # constructor for 0004  # TODO
    def zone_name(cls, ctl_id, zone_idx, name: str):
        """Constructor to set the name of a zone (c.f. parser_0004)."""

        payload = f"{zone_idx:02X}" if isinstance(zone_idx, int) else zone_idx

        payload += f"00{str_to_hex(name)[:24]:0<40}"  # TODO: check limit 12 (24)?

        return cls(" W", ctl_id, "0004", payload)

    @classmethod  # constructor for 2309
    def zone_setpoint(cls, ctl_id, zone_idx, setpoint: float):
        """Constructor to set the setpoint of a zone (c.f. parser_2309)."""
        #  W --- 34:092243 01:145038 --:------ 2309 003 0107D0

        payload = f"{zone_idx:02X}" if isinstance(zone_idx, int) else zone_idx
        payload += temp_to_hex(setpoint)

        return cls(" W", ctl_id, "2309", payload)


class FaultLog:  # 0418
    """The fault log of a system."""

    def __init__(self, ctl, msg=None, **kwargs) -> None:
        _LOGGER.debug("FaultLog(ctl=%s).__init__()", ctl)

        self._loop = ctl._gwy._loop

        self.id = ctl.id
        self._ctl = ctl
        # self._evo = ctl._evo
        self._gwy = ctl._gwy

        self._fault_log = None
        self._fault_log_done = None

        self._limit = 11  # TODO: make configurable

    def __repr_(self) -> str:
        return json.dumps(self._fault_log) if self._fault_log_done else None

    def __str_(self) -> str:
        return f"{self._ctl} (fault log)"

    @property
    def fault_log(self) -> Optional[dict]:
        """Return the fault log of a system."""
        if not self._fault_log_done:
            return

        result = {
            x: {k: v for k, v in y.items() if k[:1] != "_"}
            for x, y in self._fault_log.items()
        }

        return {k: [x for x in v.values()] for k, v in result.items()}

    async def get_fault_log(self, force_refresh=None) -> Optional[dict]:
        """Get the fault log of a system."""
        _LOGGER.debug("FaultLog(%s).get_fault_log()", self)

        self._fault_log = {}
        self._fault_log_done = None

        self._rq_log_entry(log_idx=0)  # calls loop.create_task()

        time_start = dt.now()
        while not self._fault_log_done:
            await asyncio.sleep(TIMER_SHORT_SLEEP)
            if dt.now() > time_start + TIMER_LONG_TIMEOUT * 2:
                raise ExpiredCallbackError("failed to obtain log entry (long)")

        return self.fault_log

    def _rq_log_entry(self, log_idx=0):
        """Request the next log entry."""
        _LOGGER.debug("FaultLog(%s)._rq_log_entry(%s)", self, log_idx)

        def rq_callback(msg) -> None:
            _LOGGER.debug("FaultLog(%s)._proc_log_entry(%s)", self.id, msg)

            if not msg:
                self._fault_log_done = True
                # raise ExpiredCallbackError("failed to obtain log entry (short)")
                return

            log = dict(msg.payload)
            log_idx = int(log.pop("log_idx"), 16)
            if not log:  # null response (no payload)
                # TODO: delete other callbacks rather than waiting for them to expire
                self._fault_log_done = True
                return

            self._fault_log[log_idx] = log
            if log_idx < self._limit:
                self._rq_log_entry(log_idx + 1)
            else:
                self._fault_log_done = True

        # TODO: (make method) register callback for null response (no payload)
        null_header = "|".join(("RP", self.id, "0418"))
        if null_header not in self._gwy.msg_transport._callbacks:
            self._gwy.msg_transport._callbacks[null_header] = {
                "func": rq_callback,
                "daemon": True,
                "args": [],
                "kwargs": {},
            }

        rq_callback = {"func": rq_callback, "timeout": td(seconds=10)}
        self._gwy.send_data(
            Command("RQ", self._ctl.id, "0418", f"{log_idx:06X}", callback=rq_callback)
        )


class Schedule:  # 0404
    """The schedule of a zone."""

    def __init__(self, zone, **kwargs) -> None:
        _LOGGER.debug("Schedule(zone=%s).__init__()", zone)

        self._loop = zone._gwy._loop

        self.id = zone.id
        self._zone = zone
        self.idx = zone.idx

        self._ctl = zone._ctl
        self._evo = zone._evo
        self._gwy = zone._gwy

        self._schedule = None
        self._schedule_done = None

        # initialse the fragment array()
        self._num_frags = None
        self._rx_frags = None
        self._tx_frags = None

    def __repr_(self) -> str:
        return json.dumps(self.schedule) if self._schedule_done else None

    def __str_(self) -> str:
        return f"{self._zone} (schedule)"

    @property
    def schedule(self) -> Optional[dict]:
        """Return the schedule of a zone."""
        if not self._schedule_done or None in self._rx_frags:
            return
        if self._schedule:
            return self._schedule

        if self._rx_frags[0]["msg"].payload["frag_total"] == 255:
            return {}

        frags = [v for d in self._rx_frags for k, v in d.items() if k == "fragment"]

        try:
            self._schedule = self._frags_to_sched(frags)
        except zlib.error:
            self._schedule = None
            _LOGGER.exception("Invalid schedule fragments: %s", frags)
            return

        return self._schedule

    async def get_schedule(self, force_refresh=None) -> Optional[dict]:
        """Get the schedule of a zone."""
        _LOGGER.debug(f"Schedule({self.id}).get_schedule()")

        if not await self._obtain_lock():  # TODO: should raise a TimeOut
            return

        if force_refresh:
            self._schedule_done = None

        if not self._schedule_done:
            self._rq_fragment(frag_cnt=0)  # calls loop.create_task()

            time_start = dt.now()
            while not self._schedule_done:
                await asyncio.sleep(TIMER_SHORT_SLEEP)
                if dt.now() > time_start + TIMER_LONG_TIMEOUT:
                    self._release_lock()
                    raise ExpiredCallbackError("failed to get schedule")

        self._release_lock()

        return self.schedule

    def _rq_fragment(self, frag_cnt=0) -> None:
        """Request the next missing fragment (index starts at 1, not 0)."""
        _LOGGER.debug("Schedule(%s)._rq_fragment(%s)", self.id, frag_cnt)

        def rq_callback(msg) -> None:
            if not msg:  # _LOGGER.debug()... TODO: needs fleshing out
                # TODO: remove any callbacks from msg._gwy.msg_transport._callbacks
                _LOGGER.warning(f"Schedule({self.id}): Callback timed out")
                self._schedule_done = True
                return

            _LOGGER.debug(
                f"Schedule({self.id})._proc_fragment(msg), frag_idx=%s, frag_cnt=%s",
                msg.payload.get("frag_index"),
                msg.payload.get("frag_total"),
            )

            if msg.payload["frag_total"] == 255:  # no schedule (i.e. no zone)
                _LOGGER.warning(f"Schedule({self.id}): No schedule")
                # TODO: remove any callbacks from msg._gwy.msg_transport._callbacks
                pass  # self._rx_frags = [None]

            elif msg.payload["frag_total"] != len(self._rx_frags):  # e.g. 1st frag
                self._rx_frags = [None] * msg.payload["frag_total"]

            self._rx_frags[msg.payload["frag_index"] - 1] = {
                "fragment": msg.payload["fragment"],
                "msg": msg,
            }

            # discard any fragments significantly older that this most recent fragment
            for frag in [f for f in self._rx_frags if f is not None]:
                frag = None if frag["msg"].dtm < msg.dtm - FIVE_MINS else frag

            if None in self._rx_frags:  # there are still frags to get
                self._rq_fragment(frag_cnt=msg.payload["frag_total"])
            else:
                self._schedule_done = True

        if frag_cnt == 0:
            self._rx_frags = [None]  # and: frag_idx = 0

        frag_idx = next((i for i, f in enumerate(self._rx_frags) if f is None), -1)

        # 053 RQ --- 30:185469 01:037519 --:------ 0006 001 00
        # 045 RP --- 01:037519 30:185469 --:------ 0006 004 000500E6

        # 059 RQ --- 30:185469 01:037519 --:------ 0404 007 00-23000800 0100
        # 045 RP --- 01:037519 30:185469 --:------ 0404 048 00-23000829 0104 688...
        # 059 RQ --- 30:185469 01:037519 --:------ 0404 007 00-23000800 0204
        # 045 RP --- 01:037519 30:185469 --:------ 0404 048 00-23000829 0204 4AE...
        # 059 RQ --- 30:185469 01:037519 --:------ 0404 007 00-23000800 0304
        # 046 RP --- 01:037519 30:185469 --:------ 0404 048 00-23000829 0304 6BE...

        payload = f"{self.idx}20000800{frag_idx + 1:02X}{frag_cnt:02X}"  # DHW: 23000800
        rq_callback = {"func": rq_callback, "timeout": td(seconds=1)}
        self._gwy.send_data(
            Command("RQ", self._ctl.id, "0404", payload, callback=rq_callback)
        )

    @staticmethod
    def _frags_to_sched(frags: list) -> dict:
        # _LOGGER.debug(f"Sched({self})._frags_to_sched: array is: %s", frags)
        raw_schedule = zlib.decompress(bytearray.fromhex("".join(frags)))

        zone_idx, schedule = None, []
        old_day, switchpoints = 0, []

        for i in range(0, len(raw_schedule), 20):
            zone_idx, day, time, temp, _ = struct.unpack(
                "<xxxxBxxxBxxxHxxHH", raw_schedule[i : i + 20]
            )
            if day > old_day:
                schedule.append({DAY_OF_WEEK: old_day, SWITCHPOINTS: switchpoints})
                old_day, switchpoints = day, []
            switchpoints.append(
                {
                    TIME_OF_DAY: "{0:02d}:{1:02d}".format(*divmod(time, 60)),
                    HEAT_SETPOINT: temp / 100,
                }
            )

        schedule.append({DAY_OF_WEEK: old_day, SWITCHPOINTS: switchpoints})

        return {ZONE_IDX: f"{zone_idx:02X}", SCHEDULE: schedule}

    @staticmethod
    def _sched_to_frags(schedule: dict) -> list:
        # _LOGGER.debug(f"Sched({self})._sched_to_frags: array is: %s", schedule)
        frags = [
            (
                int(schedule[ZONE_IDX], 16),
                int(week_day[DAY_OF_WEEK]),
                int(setpoint[TIME_OF_DAY][:2]) * 60 + int(setpoint[TIME_OF_DAY][3:]),
                int(setpoint[HEAT_SETPOINT] * 100),
            )
            for week_day in schedule[SCHEDULE]
            for setpoint in week_day[SWITCHPOINTS]
        ]
        frags = [struct.pack("<xxxxBxxxBxxxHxxHxx", *s) for s in frags]

        cobj = zlib.compressobj(level=9, wbits=14)
        blob = b"".join([cobj.compress(s) for s in frags]) + cobj.flush()
        blob = blob.hex().upper()

        return [blob[i : i + 82] for i in range(0, len(blob), 82)]

    async def set_schedule(self, schedule) -> None:
        """Set the schedule of a zone."""
        _LOGGER.debug(f"Schedule({self.id}).set_schedule(schedule)")

        if not await self._obtain_lock():  # TODO: should raise a TimeOut
            return

        self._schedule_done = None

        self._tx_frags = self._sched_to_frags(schedule)
        self._tx_fragment(frag_idx=0)

        time_start = dt.now()
        while not self._schedule_done:
            await asyncio.sleep(TIMER_SHORT_SLEEP)
            if dt.now() > time_start + TIMER_LONG_TIMEOUT:
                self._release_lock()
                raise ExpiredCallbackError("failed to set schedule")

        self._release_lock()

    def _tx_fragment(self, frag_idx=0) -> None:
        """Send the next fragment (index starts at 0)."""
        _LOGGER.debug(
            "Schedule(%s)._tx_fragment(%s/%s)", self.id, frag_idx, len(self._tx_frags)
        )

        def tx_callback(msg) -> None:
            _LOGGER.debug(
                f"Schedule({self.id})._proc_fragment(msg), frag_idx=%s, frag_cnt=%s",
                msg.payload.get("frag_index"),
                msg.payload.get("frag_total"),
            )

            if msg.payload["frag_index"] < msg.payload["frag_total"]:
                self._tx_fragment(frag_idx=msg.payload.get("frag_index"))
            else:
                self._schedule_done = True

        payload = "{0}200008{1:02X}{2:02d}{3:02d}{4:s}".format(
            self.idx,
            int(len(self._tx_frags[frag_idx]) / 2),
            frag_idx + 1,
            len(self._tx_frags),
            self._tx_frags[frag_idx],
        )
        tx_callback = {"func": tx_callback, "timeout": td(seconds=3)}  # 1 sec too low
        self._gwy.send_data(
            Command(" W", self._ctl.id, "0404", payload, callback=tx_callback)
        )

    async def _obtain_lock(self) -> bool:  # Lock to prevent Rx/Tx at same time
        while True:

            self._evo.zone_lock.acquire()
            if self._evo.zone_lock_idx is None:
                self._evo.zone_lock_idx = self.idx
            self._evo.zone_lock.release()

            if self._evo.zone_lock_idx == self.idx:
                break

            await asyncio.sleep(0.1)  # gives the other zone enough time

        return True

    def _release_lock(self) -> None:
        self._evo.zone_lock.acquire()
        self._evo.zone_lock_idx = None
        self._evo.zone_lock.release()
