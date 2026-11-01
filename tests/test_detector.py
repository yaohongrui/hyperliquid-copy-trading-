import unittest

from core.detector import diff_positions
from core.models import Position


def position(size, entry=1.0, mark=1.0):
    return Position("S", size, entry, mark)


class DetectorSideTests(unittest.TestCase):
    def signal(self, previous, current):
        signals = diff_positions("L", previous, current)
        self.assertEqual(len(signals), 1)
        return signals[0]

    def test_open_and_increase_report_the_new_side(self):
        opened = self.signal({}, {"S": position(100)})
        self.assertEqual((opened.kind, opened.side), ("open", "long"))

        increased = self.signal({"S": position(-20)}, {"S": position(-100)})
        self.assertEqual((increased.kind, increased.side), ("increase", "short"))

    def test_decrease_reports_the_position_being_reduced(self):
        reduced_long = self.signal({"S": position(100)}, {"S": position(40)})
        self.assertEqual((reduced_long.kind, reduced_long.side), ("decrease", "long"))

        reduced_short = self.signal({"S": position(-100)}, {"S": position(-40)})
        self.assertEqual((reduced_short.kind, reduced_short.side), ("decrease", "short"))

    def test_close_reports_the_side_being_closed(self):
        closed_long = self.signal({"S": position(100)}, {})
        self.assertEqual((closed_long.kind, closed_long.side), ("close", "long"))

        closed_short = self.signal({"S": position(-100)}, {})
        self.assertEqual((closed_short.kind, closed_short.side), ("close", "short"))

    def test_reversal_reports_the_side_being_exited(self):
        reversed_long = self.signal({"S": position(100)}, {"S": position(-100)})
        self.assertEqual((reversed_long.kind, reversed_long.side), ("close_reverse", "long"))


if __name__ == "__main__":
    unittest.main()
