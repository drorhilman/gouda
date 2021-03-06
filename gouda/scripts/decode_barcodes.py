#!/usr/bin/env python
from __future__ import print_function

import argparse
import csv
import re
import shutil
import sys
import time
import traceback

from collections import defaultdict
from itertools import count
from functools import partial

import gouda
import gouda.util

from gouda.engines.options import engine_options
from gouda.gouda_error import GoudaError
from gouda.util import expand_wildcard, read_image
from gouda.strategies.roi.roi import roi
from gouda.strategies.resize import resize


def decode(paths, strategies, engine, visitors, read_greyscale):
    """Finds and decodes barcodes in images given in pathss
    """
    for p in sorted(paths):
        if p.is_dir():
            # Descend into directory
            decode(p.iterdir(), strategies, engine, visitors, read_greyscale)
        else:
            # Process file
            try:
                img = read_image(p, read_greyscale)
                if img is None:
                    # Most likely not an image
                    for visitor in visitors:
                        visitor.result(p, [None, []])
                else:
                    # Read barcodes
                    for strategy in strategies:
                        result = strategy(img, engine)
                        if result:
                            # Found a barcode
                            break
                    else:
                        # No barcode was found
                        result = [None, []]

                    for visitor in visitors:
                        visitor.result(p, result)
            except Exception:
                print('Error processing [{0}]'.format(p))
                traceback.print_exc()


class BasicReportVisitor(object):
    """Writes a line-per-file and a line-per-barcode to stdout
    """
    def result(self, path, result):
        print(path)
        strategy, barcodes = result
        print('Found [{0}] barcodes:'.format(len(barcodes)))
        for index, barcode in enumerate(barcodes):
            print('[{0}] [{1}] [{2}]'.format(index, barcode.type, barcode.data))


class TerseReportVisitor(object):
    """Writes a line-per-file to stdout
    """
    def result(self, path, result):
        strategy, barcodes = result
        values = [b.data for b in barcodes]
        print(path, ' '.join(['[{0}]'.format(v) for v in values]))


class CSVReportVisitor(object):
    """Writes a CSV report
    """
    def __init__(self, engine, greyscale, file=None):
        self.w = csv.writer(file if file else sys.stdout, lineterminator='\n')
        self.w.writerow([
            'OS', 'Engine', 'Directory', 'File', 'Image.conversion',
            'Elapsed', 'N.found', 'Types', 'Values', 'Strategy'
        ])
        self.engine = engine
        self.image_conversion = 'Greyscale' if greyscale else 'Unchanged'
        self.start_time = time.time()

    def result(self, path, result):
        strategy, barcodes = result
        types = '|'.join(b.type for b in barcodes)
        # data could be either str or bytes
        values = '|'.join(
            b.data.decode() if hasattr(b.data, 'decode') else b.data
            for b in barcodes
        )

        self.w.writerow([sys.platform,
                         self.engine,
                         path.parent.name,
                         path.name,
                         self.image_conversion,
                         time.time()-self.start_time,
                         len(barcodes),
                         types,
                         values,
                         strategy])


class RenameVisitor(object):
    """Renames files based on their barcodes
    """
    def __init__(self, avoid_collisions):
        self.avoid_collisions = avoid_collisions
        # Mapping from path to iterator of integer suffixes, used to avoid
        # collisions - see self._destination
        self.suffix = defaultdict(partial(count, start=1))

    def _destination(self, path):
        """Returns path possibly with a suffix appended to the name to avoid
        collisions with existing files, iff self.avoid_collisions is True,
        otherwise path is returned unaltered.
        """
        destination = path
        if self.avoid_collisions:
            while destination.is_file():
                fname = '{0}-{1}{2}'.format(
                    path.stem,
                    next(self.suffix[path.name]),
                    path.suffix
                )
                destination = path.with_name(fname)

        return destination

    def result(self, path, result):
        strategy, barcodes = result
        print(path)
        if not barcodes:
            print('  No barcodes')
        else:
            # TODO How best to sanitize filenames?
            values = [
                re.sub('[^a-zA-Z0-9_-]', '_', b.data.decode()) for b in barcodes
            ]

            # The first time round the loop, the file will be renamed and
            # first_destination set to the new filename.
            # On subsequent iterations, first_destination will copied
            # to new destinations.
            first_destination = None
            for value in values:
                dest = path.with_name('{0}{1}'.format(value, path.suffix))
                dest = self._destination(dest)
                source = first_destination if first_destination else path
                rename = not bool(first_destination)
                if source == dest:
                    print('  Already correctly named')
                elif dest.is_file():
                    msg = '  Cannot rename to [{0}] because destination exists'
                    print(msg.format(dest))
                elif rename:
                    path.rename(dest)
                    print('  Renamed to [{0}]'.format(dest))
                else:
                    shutil.copy2(str(source), str(dest))
                    print('  Copied to [{0}]'.format(dest))
                if not first_destination:
                    first_destination = dest


def main(args):
    # TODO LH ROI candidate area max and/or min?
    # TODO Give area min and max as percentage of total image area?
    # TODO Report barcode regions  - both normalised and absolute coords?
    # TODO Swallow zbar warnings?

    parser = argparse.ArgumentParser(
        description='Finds and decodes barcodes on images'
    )
    parser.add_argument('--debug', '-d', action='store_true')
    parser.add_argument(
        '--action', '-a',
        choices=['basic', 'terse', 'csv', 'rename'], default='basic'
    )
    parser.add_argument('--greyscale', '-g', action='store_true')
    parser.add_argument(
        '--avoid-collisions', action='store_true',
        help=('If the action is "rename", appends a suffix to renamed files to '
              'prevent collisions')
    )

    options = engine_options()
    if not options:
        raise GoudaError('No engines are available')
    parser.add_argument('engine', choices=sorted(options.keys()))

    parser.add_argument('image', nargs='+', help='path to an image or directory')
    parser.add_argument('-v', '--version', action='version',
                        version='%(prog)s ' + gouda.__version__)

    args = parser.parse_args(args)

    gouda.util.DEBUG_PRINT = args.debug

    engine = options[args.engine]()

    if 'csv' == args.action:
        visitor = CSVReportVisitor(args.engine, args.greyscale)
    elif 'terse' == args.action:
        visitor = TerseReportVisitor()
    elif 'rename' == args.action:
        visitor = RenameVisitor(args.avoid_collisions)
    else:
        visitor = BasicReportVisitor()

    strategies = [resize, roi]
    decode(expand_wildcard(args.image), strategies, engine, [visitor],
           args.greyscale)


if __name__ == '__main__':
    main(sys.argv[1:])
