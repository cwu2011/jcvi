#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Reference-free tGBS related functions.
"""

import os.path as op
import logging
import sys

from itertools import combinations

from jcvi.formats.fasta import Fasta
from jcvi.formats.base import must_open
from jcvi.utils.counter import Counter
from jcvi.apps.cdhit import uclust, deduplicate
from jcvi.apps.base import OptionParser, ActionDispatcher, need_update, sh, iglob


class HaplotypeResolver (object):

    def __init__(self, haplotype_set):
        self.haplotype_set = haplotype_set
        self.nind = len(haplotype_set)
        self.counter = Counter()
        for haplotypes in haplotype_set:
            self.counter.update(Counter(haplotypes))

    def __str__(self):
        return str(self.counter)

    def solve(self, fw):
        haplotype_counts = self.counter.items()
        for (a, ai), (b, bi) in combinations(haplotype_counts, 2):
            abi = sum(1 for haplotypes in self.haplotype_set \
                if a in haplotypes and b in haplotypes)
            print >> fw, a, b, abi
            fw.flush()


def main():

    actions = (
        ('snp', 'run SNP calling on GSNAP output'),
        ('bam', 'convert GSNAP output to BAM'),
        ('novo', 'reference-free tGBS pipeline'),
        ('resolve', 'separate repeats on collapsed contigs'),
        ('count', 'count the number of reads in all clusters'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def resolve(args):
    """
    %prog resolve matrixfile fastafile bamfolder

    Separate repeats along collapsed contigs. First scan the matrixfile for
    largely heterozygous sites. For each heterozygous site, we scan each bam to
    retrieve distinct haplotypes. The frequency of each haplotype is then
    computed, the haplotype with the highest frequency, assumed to be
    paralogous, is removed.
    """
    import pysam
    from collections import defaultdict
    from itertools import groupby

    p = OptionParser(resolve.__doc__)
    p.add_option("--missing", default=.5,
                 help="Maximum level of missing data")
    p.add_option("--het", default=.5,
                 help="Maximum level of heterozygous calls")
    p.set_outfile()
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    matrixfile, fastafile, bamfolder = args
    #f = Fasta(fastafile)
    fp = open(matrixfile)
    for row in fp:
        if row[0] != '#':
            break
    header = row.split()
    ngenotypes = len(header) - 4
    nmissing = int(round(opts.missing * ngenotypes))
    logging.debug("A total of {0} individuals scanned".format(ngenotypes))
    logging.debug("Look for markers with < {0} missing and > {1} het".\
                    format(opts.missing, opts.het))
    bamfiles = iglob(bamfolder, "*.bam")
    logging.debug("Folder `{0}` contained {1} bam files".\
                    format(bamfolder, len(bamfiles)))

    data = []
    for row in fp:
        if row[0] == '#':
            continue
        atoms = row.split()
        seqid, pos, ref, alt = atoms[:4]
        genotypes = atoms[4:]
        c = Counter(genotypes)
        c0 = c.get('0', 0)
        c3 = c.get('3', 0)
        if c0 >= nmissing:
            continue
        hetratio = c3 * 1. / (ngenotypes - c0)
        if hetratio <= opts.het:
            continue
        pos = int(pos)
        data.append((seqid, pos, ref, alt, c, hetratio))

    data.sort()
    logging.debug("A total of {0} target markers in {1} contigs.".\
                    format(len(data), len(set(x[0] for x in data))))
    samfiles = [pysam.AlignmentFile(x, "rb") for x in bamfiles]

    fw = must_open(opts.outfile, "w")
    for seqid, d in groupby(data, lambda x: x[0]):
        d = list(d)
        nmarkers = len(d)
        logging.debug("Process contig {0} ({1} markers)".format(seqid, nmarkers))
        haplotype_set = []
        for samfile in samfiles:
            reads = defaultdict(list)
            positions = []
            for s, pos, ref, alt, c, hetratio in d:
                for c in samfile.pileup(seqid):
                    if c.reference_pos != pos - 1:
                        continue
                    for r in c.pileups:
                        rname = r.alignment.query_name
                        rbase = r.alignment.query_sequence[r.query_position]
                        reads[rname].append((pos, rbase))
                positions.append(pos)
            haplotypes = []
            for read in reads.values():
                hap = ['-'] * nmarkers
                for p, rbase in read:
                    hap[positions.index(p)] = rbase
                hap = "".join(hap)
                if "-" in hap:
                    continue
                haplotypes.append(hap)
            haplotypes = set(haplotypes)
            haplotype_set.append(haplotypes)
        hr = HaplotypeResolver(haplotype_set)
        print >> fw, seqid, hr
        hr.solve(fw)


def count(args):
    """
    %prog count cdhit.consensus.fasta

    Scan the headers for the consensus clusters and count the number of reads.
    """
    from jcvi.graphics.histogram import stem_leaf_plot
    from jcvi.utils.cbook import SummaryStats

    p = OptionParser(count.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    fastafile, = args
    f = Fasta(fastafile, lazy=True)
    sizes = []
    for desc, rec in f.iterdescriptions_ordered():
        if desc.startswith("singleton"):
            sizes.append(1)
            continue
        # consensus_for_cluster_0 with 63 sequences
        name, w, size, seqs = desc.split()
        assert w == "with"
        sizes.append(int(size))

    s = SummaryStats(sizes)
    print >> sys.stderr, s
    stem_leaf_plot(s.data, 0, 100, 20, title="Cluster size")


def novo(args):
    """
    %prog novo reads.fastq

    Reference-free tGBS pipeline.
    """
    from jcvi.assembly.kmer import jellyfish, histogram
    from jcvi.assembly.preprocess import diginorm
    from jcvi.formats.fasta import filter as fasta_filter, format
    from jcvi.apps.cdhit import filter as cdhit_filter

    p = OptionParser(novo.__doc__)
    p.add_option("--technology", choices=("illumina", "454", "iontorrent"),
                 default="iontorrent", help="Sequencing platform")
    p.add_option("--dedup", choices=("uclust", "cdhit"),
                 default="cdhit", help="Dedup algorithm")
    p.set_depth(depth=50)
    p.set_align(pctid=96)
    p.set_home("cdhit")
    p.set_home("fiona")
    p.set_cpus()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    fastqfile, = args
    cpus = opts.cpus
    depth = opts.depth
    pf, sf = fastqfile.rsplit(".", 1)

    diginormfile = pf + ".diginorm." + sf
    if need_update(fastqfile, diginormfile):
        diginorm([fastqfile, "--single", "--depth={0}".format(depth)])
        keepabund = fastqfile + ".keep.abundfilt"
        sh("cp -s {0} {1}".format(keepabund, diginormfile))

    jf = pf + "-K23.histogram"
    if need_update(diginormfile, jf):
        jellyfish([diginormfile, "--prefix={0}".format(pf),
                    "--cpus={0}".format(cpus)])

    genomesize = histogram([jf, pf, "23"])
    fiona = pf + ".fiona.fa"
    if need_update(diginormfile, fiona):
        cmd = op.join(opts.fiona_home, "bin/fiona")
        cmd += " -g {0} -nt {1} --sequencing-technology {2}".\
                    format(genomesize, cpus, opts.technology)
        cmd += " -vv {0} {1}".format(diginormfile, fiona)
        logfile = pf + ".fiona.log"
        sh(cmd, outfile=logfile, errfile=logfile)

    dedup = opts.dedup
    pctid = opts.pctid
    cons = fiona + ".P{0}.{1}.consensus.fasta".format(pctid, dedup)
    if need_update(fiona, cons):
        if dedup == "cdhit":
            deduplicate([fiona, "--consensus", "--reads",
                         "--pctid={0}".format(pctid),
                         "--cdhit_home={0}".format(opts.cdhit_home)])
        else:
            uclust([fiona, "--pctid={0}".format(pctid)])

    filteredfile = pf + ".filtered.fasta"
    if need_update(cons, filteredfile):
        covfile = pf + ".cov.fasta"
        cdhit_filter([cons, "--outfile={0}".format(covfile),
                      "--minsize={0}".format(depth / 5)])
        fasta_filter([covfile, "50", "--outfile={0}".format(filteredfile)])

    finalfile = pf + ".final.fasta"
    if need_update(filteredfile, finalfile):
        format([filteredfile, finalfile, "--sequential=replace",
                    "--prefix={0}_".format(pf)])


def bam(args):
    """
    %prog snp input.gsnap ref.fasta

    Convert GSNAP output to BAM.
    """
    from jcvi.formats.sizes import Sizes
    from jcvi.formats.sam import index

    p = OptionParser(bam.__doc__)
    p.set_home("eddyyeh")
    p.set_cpus()
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    gsnapfile, fastafile = args
    EYHOME = opts.eddyyeh_home
    pf = gsnapfile.rsplit(".", 1)[0]
    uniqsam = pf + ".unique.sam"
    if need_update((gsnapfile, fastafile), uniqsam):
        cmd = op.join(EYHOME, "gsnap2gff3.pl")
        sizesfile = Sizes(fastafile).filename
        cmd += " --format sam -i {0} -o {1}".format(gsnapfile, uniqsam)
        cmd += " -u -l {0} -p {1}".format(sizesfile, opts.cpus)
        sh(cmd)

    index([uniqsam])


def snp(args):
    """
    %prog snp input.gsnap

    Run SNP calling on GSNAP output after apps.gsnap.align().
    """
    p = OptionParser(snp.__doc__)
    p.set_home("eddyyeh")
    p.set_cpus()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gsnapfile, = args
    EYHOME = opts.eddyyeh_home
    pf = gsnapfile.rsplit(".", 1)[0]
    nativefile = pf + ".native"
    if need_update(gsnapfile, nativefile):
        cmd = op.join(EYHOME, "convert2native.pl")
        cmd += " --gsnap {0} -o {1}".format(gsnapfile, nativefile)
        cmd += " -proc {0}".format(opts.cpus)
        sh(cmd)

    snpfile = pf + ".snp"
    if need_update(nativefile, snpfile):
        cmd = op.join(EYHOME, "SNPs/SNP_Discovery-short.pl")
        cmd += " --native {0} -o {1}".format(nativefile, snpfile)
        cmd += " -a 2 -ac 0.3 -c 0.8"
        sh(cmd)


if __name__ == '__main__':
    main()
