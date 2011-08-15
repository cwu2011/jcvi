#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
CLC bio assembly file CAS, and the tabular format generated by `assembly_table
-n -s -p`
"""

import os
import os.path as op
import sys
import logging

from collections import defaultdict
from itertools import groupby
from optparse import OptionParser

from Bio import SeqIO

from jcvi.formats.base import LineFile
from jcvi.formats.blast import set_options_pairs, report_pairs
from jcvi.apps.base import ActionDispatcher, sh, set_grid, debug, is_newer_file
debug()


class CasTabLine (LineFile):
    """
    The table generate by command `assembly_table -n -s -p`
    from clcbio assembly cell
    """
    def __init__(self, line):
        args = line.split()
        self.readnum = args[0]  # usually integer or `-`
        self.readname = args[1]
        self.readlen = int(args[-10])
        # 0-based indexing
        self.readstart = int(args[-9])
        if self.readstart >= 0:
            self.readstart += 1

        self.readstop = int(args[-8])
        self.refnum = int(args[-7])

        self.refstart = int(args[-6])
        if self.refstart >= 0:
            self.refstart += 1

        self.refstop = int(args[-5])

        self.is_reversed = (int(args[-4]) == 1)
        self.strand = '-' if self.is_reversed else '+'

        self.nummatches = int(args[-3])
        self.is_paired = (int(args[-2]) == 1)
        self.score = int(args[-1])

    @property
    def bedline(self):
        return "\t".join(str(x) for x in (self.refnum,
            self.refstart - 1, self.refstop, self.readname,
            self.score, self.strand))


def main():

    actions = (
        ('txt', "convert CAS file to tabular output using assembly_table"),
        ('split', 'split CAS file into smaller CAS using sub_assembly'),
        ('bed', 'convert cas tabular output to bed format'),
        ('pairs', 'print paired-end reads of cas tabular output'),
        ('info', 'print the number of read mappig using `assembly_info`'),
        ('fastpairs', 'print pair distance and orientation, assuming paired '\
            'reads next to one another'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def info(args):
    """
    %prog info casfile

    Wraps around `assembly_info` and get the following block.

    General info:
    Read info:
    Coverage info:

    In particular, the read info will be reorganized so that it shows the
    percentage of unmapped, mapped, unique and multi-hit reads.
    """
    from jcvi.apps.base import popen
    from jcvi.utils.cbook import percentage

    p = OptionParser(info.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    casfile, = args
    cmd = "assembly_info {0}".format(casfile)
    fp = popen(cmd)
    inreadblock = False
    for row in fp:
        if row.startswith("Contig info:"):
            break

        if row.startswith("Read info:"):
            inreadblock = True

        srow = row.strip()

        # Following looks like a hack, but to keep compatible between
        # CLC 3.20 and CLC 4.0 beta
        if inreadblock:
            atoms = row.split('s')
            last = atoms[-1].split()[0] if len(atoms) > 1 else "0"

        if srow.startswith("Reads"):
            reads = int(last)
        if srow.startswith("Unmapped") or srow.startswith("Unassembled"):
            unmapped = int(last)
        if srow.startswith("Mapped") or srow.startswith("Assembled"):
            mapped = int(last)
        if srow.startswith("Multi"):
            multihits = int(last)

        if row.startswith("Coverage info:"):
            # Print the Read info: block
            print "Read info:"
            assert mapped + unmapped == reads

            unique = mapped - multihits
            print
            print "Total reads: {0}".format(reads)
            print "Unmapped reads: {0}".format(percentage(unmapped, reads, False))
            print "Mapped reads: {0}".format(percentage(mapped, reads, False))
            print "Unique reads: {0}".format(percentage(unique, reads, False))
            print "Multi hit reads: {0}".\
                    format(percentage(multihits, reads, False))
            print
            inreadblock = False

        if not inreadblock:
            print row.rstrip()


def fastpairs(args):
    """
    %prog fastpairs castabfile

    Assuming paired reads are adjacent in the castabfile. Print pair distance
    and orientations.
    """
    from jcvi.utils.range import range_distance
    from jcvi.assembly.base import orientationlabels

    p = OptionParser(fastpairs.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    castabfile, = args
    fp = open(castabfile)
    arow = fp.readline()
    while arow:
        brow = fp.readline()
        a, b = CasTabLine(arow), CasTabLine(brow)
        asubject, astart, astop = a.refnum, a.refstart, a.refstop
        bsubject, bstart, bstop = b.refnum, b.refstart, b.refstop
        if -1 not in (astart, bstart):
            aquery, bquery = a.readname, b.readname
            astrand, bstrand = a.strand, b.strand
            dist, orientation = range_distance(\
                (asubject, astart, astop, astrand),
                (bsubject, bstart, bstop, bstrand)
                    )
            orientation = orientationlabels[orientation]
            if dist != -1:
                print "\t".join(str(x) for x in (aquery, bquery, dist, orientation))
        arow = fp.readline()


def txt(args):
    """
    %prog txt casfile

    convert binary CAS file to tabular output using CLC assembly_table
    """
    p = OptionParser(txt.__doc__)
    p.add_option("-m", dest="multi", default=False, action="store_true",
        help="report multi-matches [default: %default]")
    set_grid(p)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    grid = opts.grid

    casfile, = args
    txtfile = casfile.replace(".cas", ".txt")
    assert op.exists(casfile)

    cmd = "assembly_table -n -s -p "
    if opts.multi:
        cmd += "-m "
    cmd += casfile
    sh(cmd, grid=grid, outfile=txtfile)

    return txtfile


def split(args):
    """
    %prog split casfile 1 10

    split the binary casfile by using CLCbio `sub_assembly` program, the two
    numbers are starting and ending index for the `reference`; useful to split
    one big assembly per contig
    """
    p = OptionParser(split.__doc__)
    set_grid(p)

    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(p.print_help())

    casfile, start, end = args
    start = int(start)
    end = int(end)

    split_cmd = "sub_assembly -a {casfile} -o sa.{i}.cas -s {i} " + \
        "-e sa.{i}.pairs.fasta -f sa.{i}.fragments.fasta -g sa.{i}.ref.fasta"

    for i in range(start, end + 1):
        cmd = split_cmd.format(casfile=casfile, i=i)
        sh(cmd, grid=opts.grid)


def check_txt(casfile):
    """
    Check to see if the casfile is already converted to txtfile with txt().
    """
    if casfile.endswith(".cas"):
        castabfile = casfile.replace(".cas", ".txt")
        if not is_newer_file(castabfile, casfile):
            castabfile = txt([casfile])
        else:
            logging.debug("File `{0}` found.".format(castabfile))
    else:
        castabfile = casfile

    return castabfile


def bed(args):
    """
    %prog bed casfile fastafile

    convert the CAS or CASTAB format into bed format
    """
    p = OptionParser(bed.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    casfile, fastafile = args
    castabfile = check_txt(casfile)

    refnames = [rec.id for rec in SeqIO.parse(fastafile, "fasta")]
    fp = open(castabfile)
    bedfile = castabfile.rsplit(".", 1)[0] + ".bed"
    fw = open(bedfile, "w")
    for row in fp:
        b = CasTabLine(row)
        if b.readstart == -1:
            continue
        b.refnum = refnames[b.refnum]
        print >> fw, b.bedline

    logging.debug("File written to `{0}`.".format(bedfile))


def pairs(args):
    """
    See __doc__ for set_options_pairs().
    """
    p = set_options_pairs()

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    casfile, = args
    castabfile = check_txt(casfile)

    basename = castabfile.split(".")[0]
    pairsfile = ".".join((basename, "pairs")) if opts.pairsfile else None
    insertsfile = ".".join((basename, "inserts")) if opts.insertsfile else None

    fp = open(castabfile)
    data = [CasTabLine(row) for i, row in enumerate(fp) if i < opts.nrows]

    ascii = not opts.pdf
    return report_pairs(data, opts.cutoff, opts.mateorientation,
           dialect="castab", pairsfile=pairsfile, insertsfile=insertsfile,
           rclip=opts.rclip, ascii=ascii, bins=opts.bins)


if __name__ == '__main__':
    main()
