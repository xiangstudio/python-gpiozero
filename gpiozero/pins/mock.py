from __future__ import (
    unicode_literals,
    absolute_import,
    print_function,
    division,
    )
str = type('')


from collections import namedtuple
from time import time, sleep
from threading import Thread, Event
try:
    from math import isclose
except ImportError:
    from ..compat import isclose

from . import Pin
from .data import pi_info
from ..exc import PinSetInput, PinPWMUnsupported, PinFixedPull


PinState = namedtuple('PinState', ('timestamp', 'state'))

class MockPin(Pin):
    """
    A mock pin used primarily for testing. This class does *not* support PWM.
    """

    _PINS = {}

    @classmethod
    def clear_pins(cls):
        cls._PINS.clear()

    @classmethod
    def pi_info(cls):
        return pi_info('a21041') # Pretend we're a Pi 2B

    def __new__(cls, number):
        if not (0 <= number < 54):
            raise ValueError('invalid pin %d specified (must be 0..53)' % number)
        try:
            old_pin = cls._PINS[number]
        except KeyError:
            self = super(MockPin, cls).__new__(cls)
            cls._PINS[number] = self
            self._number = number
            self._function = 'input'
            self._state = False
            self._pull = 'floating'
            self._bounce = None
            self._edges = 'both'
            self._when_changed = None
            self.clear_states()
            return self
        # Ensure the pin class expected supports PWM (or not)
        if issubclass(cls, MockPWMPin) != isinstance(old_pin, MockPWMPin):
            raise ValueError('pin %d is already in use as a %s' % (number, old_pin.__class__.__name__))
        return old_pin

    def __repr__(self):
        return 'MOCK%d' % self._number

    @property
    def number(self):
        return self._number

    def close(self):
        self.when_changed = None
        self.function = 'input'

    def _get_function(self):
        return self._function

    def _set_function(self, value):
        assert value in ('input', 'output')
        self._function = value
        if value == 'input':
            # Drive the input to the pull
            self._set_pull(self._get_pull())

    def _get_state(self):
        return self._state

    def _set_state(self, value):
        if self._function == 'input':
            raise PinSetInput('cannot set state of pin %r' % self)
        assert self._function == 'output'
        assert 0 <= value <= 1
        self._change_state(bool(value))

    def _change_state(self, value):
        if self._state != value:
            t = time()
            self._state = value
            self.states.append(PinState(t - self._last_change, value))
            self._last_change = t
            return True
        return False

    def _get_frequency(self):
        return None

    def _set_frequency(self, value):
        if value is not None:
            raise PinPWMUnsupported()

    def _get_pull(self):
        return self._pull

    def _set_pull(self, value):
        assert self._function == 'input'
        assert value in ('floating', 'up', 'down')
        self._pull = value
        if value == 'up':
            self.drive_high()
        elif value == 'down':
            self.drive_low()

    def _get_bounce(self):
        return self._bounce

    def _set_bounce(self, value):
        # XXX Need to implement this
        self._bounce = value

    def _get_edges(self):
        return self._edges

    def _set_edges(self, value):
        assert value in ('none', 'falling', 'rising', 'both')
        self._edges = value

    def _get_when_changed(self):
        return self._when_changed

    def _set_when_changed(self, value):
        self._when_changed = value

    def drive_high(self):
        assert self._function == 'input'
        if self._change_state(True):
            if self._edges in ('both', 'rising') and self._when_changed is not None:
                self._when_changed()

    def drive_low(self):
        assert self._function == 'input'
        if self._change_state(False):
            if self._edges in ('both', 'falling') and self._when_changed is not None:
                self._when_changed()

    def clear_states(self):
        self._last_change = time()
        self.states = [PinState(0.0, self._state)]

    def assert_states(self, expected_states):
        # Tests that the pin went through the expected states (a list of values)
        for actual, expected in zip(self.states, expected_states):
            assert actual.state == expected

    def assert_states_and_times(self, expected_states):
        # Tests that the pin went through the expected states at the expected
        # times (times are compared with a tolerance of tens-of-milliseconds as
        # that's about all we can reasonably expect in a non-realtime
        # environment on a Pi 1)
        for actual, expected in zip(self.states, expected_states):
            assert isclose(actual.timestamp, expected[0], rel_tol=0.05, abs_tol=0.05)
            assert isclose(actual.state, expected[1])


class MockPulledUpPin(MockPin):
    """
    This derivative of :class:`MockPin` emulates a pin with a physical pull-up
    resistor.
    """
    def _set_pull(self, value):
        if value != 'up':
            raise PinFixedPull('pin has a physical pull-up resistor')


class MockChargingPin(MockPin):
    """
    This derivative of :class:`MockPin` emulates a pin which, when set to
    input, waits a predetermined length of time and then drives itself high
    (as if attached to, e.g. a typical circuit using an LDR and a capacitor
    to time the charging rate).
    """
    def __init__(self, number):
        super(MockChargingPin, self).__init__()
        self.charge_time = 0.01 # dark charging time
        self._charge_stop = Event()
        self._charge_thread = None

    def _set_function(self, value):
        super(MockChargingPin, self)._set_function(value)
        if value == 'input':
            if self._charge_thread:
                self._charge_stop.set()
                self._charge_thread.join()
            self._charge_stop.clear()
            self._charge_thread = Thread(target=self._charge)
            self._charge_thread.start()
        elif value == 'output':
            if self._charge_thread:
                self._charge_stop.set()
                self._charge_thread.join()

    def _charge(self):
        if not self._charge_stop.wait(self.charge_time):
            try:
                self.drive_high()
            except AssertionError:
                # Charging pins are typically flipped between input and output
                # repeatedly; if another thread has already flipped us to
                # output ignore the assertion-error resulting from attempting
                # to drive the pin high
                pass


class MockTriggerPin(MockPin):
    """
    This derivative of :class:`MockPin` is intended to be used with another
    :class:`MockPin` to emulate a distance sensor. Set :attr:`echo_pin` to the
    corresponding pin instance. When this pin is driven high it will trigger
    the echo pin to drive high for the echo time.
    """
    def __init__(self, number):
        super(MockTriggerPin, self).__init__()
        self.echo_pin = None
        self.echo_time = 0.04 # longest echo time
        self._echo_thread = None

    def _set_state(self, value):
        super(MockTriggerPin, self)._set_state(value)
        if value:
            if self._echo_thread:
                self._echo_thread.join()
            self._echo_thread = Thread(target=self._echo)
            self._echo_thread.start()

    def _echo(self):
        sleep(0.001)
        self.echo_pin.drive_high()
        sleep(self.echo_time)
        self.echo_pin.drive_low()


class MockPWMPin(MockPin):
    """
    This derivative of :class:`MockPin` adds PWM support.
    """
    def __init__(self, number):
        super(MockPWMPin, self).__init__()
        self._frequency = None

    def close(self):
        self.frequency = None
        super(MockPWMPin, self).close()

    def _set_state(self, value):
        if self._function == 'input':
            raise PinSetInput('cannot set state of pin %r' % self)
        assert self._function == 'output'
        assert 0 <= value <= 1
        self._change_state(float(value))

    def _get_frequency(self):
        return self._frequency

    def _set_frequency(self, value):
        if value is not None:
            assert self._function == 'output'
        self._frequency = value
        if value is None:
            self._change_state(0.0)


class MockSPIClockPin(MockPin):
    """
    This derivative of :class:`MockPin` is intended to be used as the clock pin
    of a mock SPI device. It is not intended for direct construction in tests;
    rather, construct a :class:`MockSPIDevice` with various pin numbers, and
    this class will be used for the clock pin.
    """
    def __init__(self, number):
        super(MockSPIClockPin, self).__init__()
        if not hasattr(self, 'spi_devices'):
            self.spi_devices = []

    def _set_state(self, value):
        super(MockSPIClockPin, self)._set_state(value)
        for dev in self.spi_devices:
            dev.on_clock()


class MockSPISelectPin(MockPin):
    """
    This derivative of :class:`MockPin` is intended to be used as the select
    pin of a mock SPI device. It is not intended for direct construction in
    tests; rather, construct a :class:`MockSPIDevice` with various pin numbers,
    and this class will be used for the select pin.
    """
    def __init__(self, number):
        super(MockSPISelectPin, self).__init__()
        if not hasattr(self, 'spi_device'):
            self.spi_device = None

    def _set_state(self, value):
        super(MockSPISelectPin, self)._set_state(value)
        if self.spi_device:
            self.spi_device.on_select()


class MockSPIDevice(object):
    def __init__(
            self, clock_pin, mosi_pin, miso_pin, select_pin=None,
            clock_polarity=False, clock_phase=False, lsb_first=False,
            bits_per_word=8, select_high=False):
        self.clock_pin = MockSPIClockPin(clock_pin)
        self.mosi_pin = None if mosi_pin is None else MockPin(mosi_pin)
        self.miso_pin = None if miso_pin is None else MockPin(miso_pin)
        self.select_pin = None if select_pin is None else MockSPISelectPin(select_pin)
        self.clock_polarity = clock_polarity
        self.clock_phase = clock_phase
        self.lsb_first = lsb_first
        self.bits_per_word = bits_per_word
        self.select_high = select_high
        self.rx_bit = 0
        self.rx_buf = []
        self.tx_buf = []
        self.clock_pin.spi_devices.append(self)
        self.select_pin.spi_device = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.close()

    def close(self):
        if self in self.clock_pin.spi_devices:
            self.clock_pin.spi_devices.remove(self)
        if self.select_pin is not None:
            self.select_pin.spi_device = None

    def on_select(self):
        if self.select_pin.state == self.select_high:
            self.on_start()

    def on_clock(self):
        # Don't do anything if this SPI device isn't currently selected
        if self.select_pin is None or self.select_pin.state == self.select_high:
            # The XOR of the clock pin's values, polarity and phase indicates
            # whether we're meant to be acting on this edge
            if self.clock_pin.state ^ self.clock_polarity ^ self.clock_phase:
                self.rx_bit += 1
                if self.mosi_pin is not None:
                    self.rx_buf.append(self.mosi_pin.state)
                if self.miso_pin is not None:
                    try:
                        tx_value = self.tx_buf.pop(0)
                    except IndexError:
                        tx_value = 0
                    if tx_value:
                        self.miso_pin.drive_high()
                    else:
                        self.miso_pin.drive_low()
                self.on_bit()

    def on_start(self):
        """
        Override this in descendents to detect when the mock SPI device's
        select line is activated.
        """
        self.rx_bit = 0
        self.rx_buf = []
        self.tx_buf = []

    def on_bit(self):
        """
        Override this in descendents to react to receiving a bit.

        The :attr:`rx_bit` attribute gives the index of the bit received (this
        is reset to 0 by default by :meth:`on_select`). The :attr:`rx_buf`
        sequence gives the sequence of 1s and 0s that have been recevied so
        far. The :attr:`tx_buf` sequence gives the sequence of 1s and 0s to
        transmit on the next clock pulses. All these attributes can be modified
        within this method.

        The :meth:`rx_word` and :meth:`tx_word` methods can also be used to
        read and append to the buffers using integers instead of bool bits.
        """
        pass

    def rx_word(self):
        result = 0
        bits = reversed(self.rx_buf) if self.lsb_first else self.rx_buf
        for bit in bits:
            result <<= 1
            result |= bit
        return result

    def tx_word(self, value, bits_per_word=None):
        if bits_per_word is None:
            bits_per_word = self.bits_per_word
        bits = [0] * bits_per_word
        for bit in range(bits_per_word):
            bits[bit] = value & 1
            value >>= 1
        assert not value
        if not self.lsb_first:
            bits = reversed(bits)
        self.tx_buf.extend(bits)

