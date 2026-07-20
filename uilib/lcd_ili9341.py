# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of pi-stomp.
#
# pi-stomp is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pi-stomp is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pi-stomp.  If not, see <https://www.gnu.org/licenses/>.

from uilib.panel import LcdBase, Box
from functools import cached_property
import logging
import threading
import os
import queue

# Written by early-boot splash integration before pi-stomp starts; lives in tmpfs
# (/run) for the current boot only. pi-stomp reads it but never creates it.
INIT_STAMP = "/run/lcd.init"


class LcdIli9341(LcdBase):
    # XXX
    # TODO: Turn "flip" into all 90deg angle combinations
    def __init__(self, spi, cs_pin, dc_pin, reset_pin, baudrate, flip=True):
        import adafruit_rgb_display.ili9341 as ili9341
        self.disp = ili9341.ILI9341(
            spi, cs=cs_pin, dc=dc_pin, rst=reset_pin, baudrate=baudrate
        )

        # Use this to assure we don't have multiple threads trying to change the screen
        # All methods which do change the screen (eg. dist. calls) should acquire/release
        self.lock = threading.Lock()

        # Always reset and clear on process start so service restarts are reliable.
        # has_system_splash (INIT_STAMP) only skips the in-app splash in lcd320x240.
        self.clear()

        # Test full screen image
        self.width = self.disp.height
        self.height = self.disp.width
        self.flip = flip

        # Initialize queue and writer thread for asynchronous display updates
        self._queue = queue.Queue()
        self._running = True
        self._thread = threading.Thread(target=self._write_loop, daemon=True, name="lcd-writer")
        self._thread.start()

    @cached_property
    def has_system_splash(self):
        """True when early boot left INIT_STAMP (OS splash already shown this boot)."""
        return os.path.exists(INIT_STAMP)

    def dimensions(self):
        return (self.width, self.height)

    def default_format(self):
        return "RGB"

    def clear(self):
        self.lock.acquire()
        self.disp.fill(0)
        self.lock.release()

    def update(self, image, box = None):
        img_width, img_height = image.size
        if box is None:
            box = Box(0, 0, img_width, img_height)

        # Check if we need to crop the image to the LCD size
        x1, y1, x2, y2 = box.rect
        if x2 > self.width:
            x2 = self.width
        if y2 > self.height:
            y2 = self.height

        # Crop the portion of the image to send and force pixel copy immediately
        sub_image = image.crop((x1, y1, x2, y2))
        sub_image.load()

        if self.flip:
            x = self.height - y2
            y = x1
        else:
            x = y1
            y = self.width - x2

        self._queue.put((sub_image, x, y))

    def _write_loop(self):
        while self._running:
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                break
            sub_image, x, y = task
            self.lock.acquire()
            try:
                self.disp.image(sub_image, 270 if self.flip else 90, x, y)
            except Exception as e:
                logging.error(f"LCD SPI background write failed: {e}")
            finally:
                self.lock.release()
            self._queue.task_done()

    def cleanup(self):
        self._running = False
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

