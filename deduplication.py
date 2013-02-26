# -*- coding: utf-8 -*-

# Copyright 2011 Mats Ekberg
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import with_statement

from common import *
from jsonrpc import FileDataSource

import sys
from rollingcs import RollingChecksum, calc_rolling
import tempfile

class BlockChecksum:
    def __init__(self, window_size, tmpdir = None):
        self.buffer = FileAsString(tempfile.SpooledTemporaryFile(max_size=2**23, dir=tmpdir))
        self.window_size = window_size
        self.position = 0
        self.blocks = []

    def feed_string(self, s):
        self.buffer.append(s)
        while len(self.buffer) - self.position >= self.window_size:
            block = self.buffer[self.position:self.position+self.window_size]
            block_md5 = md5sum(block)
            block_rolling = calc_rolling(block, self.window_size)
            self.blocks.append((self.position, block_rolling, block_md5))
            self.position += self.window_size

    def harvest(self):
        result = self.blocks
        self.blocks = []
        return result
            
class UniformBlobGetter:
    def __init__(self, repo, local_blob_dir = None):
        self.repo = repo
        self.local_blob_dir = local_blob_dir

    def get_blob_size(self, blob_name):
        assert is_md5sum(blob_name)
        if self.local_blob_dir:
            local_path = os.path.join(self.local_blob_dir, blob_name)
            if os.path.exists(local_path):
                return long(os.path.getsize(local_path))
        return self.front.get_blob_size(blob_name)

    def get_blob_reader(self, blob_name, offset, size):
        assert is_md5sum(blob_name)
        if self.local_blob_dir:
            local_path = os.path.join(self.local_blob_dir, blob_name)
            if os.path.exists(local_path):
                fo = safe_open(local_path, "rb")
                fo.seek(offset)                
                return FileDataSource(fo, size)
        return self.repo.get_blob_reader(blob_name, offset, size)

class TailBuffer:
    """ A buffer that only physically keeps the last tail_size bytes
    of the data that is appended to it, but can be accessed using the
    positions of the original data. """

    def __init__(self, tail_size, tmpdir = None):
        #self.tail_size = tail_size
        self.tmpfo = tempfile.SpooledTemporaryFile(max_size=2**23, dir = tmpdir)
        self.buffer = FileAsString(self.tmpfo)
        
    def append(self, s):
        self.buffer.append(s)

    def get_blocks(self, start, size):
        return file_reader(self.tmpfo, start, start + size)

    def __len__(self):
        return len(self.buffer)

    def __getitem__(self, index):
        return self.buffer.__getitem__(index)

class OriginalPieceHandler:
    def init_piece(self, index):
        pass

    def add_piece_data(self, index, data):
        pass
    
    def end_piece(self, index):
        pass    

START = "START"
ORIGINAL = "ORIGINAL"
MATCH = "MATCH"
END = "END"

class RecipeFinder:
    def __init__(self, blocksdb, block_size, intset, blob_source, original_piece_handler = OriginalPieceHandler(), tmpdir = None):
        self.blocksdb = blocksdb
        self.block_size = block_size
        self.rs = RollingChecksum(block_size, intset)
        self.blob_source = blob_source
        self.original_piece_handler = original_piece_handler

        self.tail_buffer = TailBuffer(self.block_size*2, tmpdir = tmpdir)
        self.end_of_last_hit = 0 # The end of the last matched block
        self.original_run_since = None
        self.addresses = []
        self.md5summer = hashlib.md5()
        self.md5summer_reconstruct = hashlib.md5()
        #self.reconstructed_file = tempfile.NamedTemporaryFile(delete=False)
        #print "Reconstructed file is", self.reconstructed_file.name
        self.original_piece_count = 0
        self.closed = False
        self.feed_byte_count = 0

        self.seqfinder = self.blocksdb.get_sequence_finder()
        self.dedup_state = START
        self.dedup_last_border = 0
        self.dedup_last_offset = 0
        self.seq_number = 0

    def __enter_state(self, state, offset):
        assert state in (MATCH, ORIGINAL, END)
        assert offset >= self.dedup_last_border
        if self.dedup_state == ORIGINAL:
            print "Original data (seq %s): '%s'" % (self.seq_number, self.tail_buffer[self.dedup_last_offset:offset])
        elif self.dedup_state == MATCH:
            print "Matched data (seq %s): '%s'" % (self.seq_number, self.tail_buffer[self.dedup_last_offset:offset])
        if state != self.dedup_state:
            print "Border at %s (from %s to %s)" % (offset, self.dedup_state, state)
            self.dedup_state = state
            self.dedup_last_border = offset
            self.seq_number += 1
        self.dedup_last_offset = offset

    def feed(self, s):
        #assert len(s) <= self.block_size, "%s %s" % (s, self.block_size)
        assert not self.closed
        self.feed_byte_count += len(s)
        self.rs.feed_string(s)
        self.md5summer.update(s)
        self.tail_buffer.append(s)
        for offset, rolling in self.rs:
            if offset < self.end_of_last_hit:
                # Ignore overlapping blocks
                continue
            md5 = md5sum(self.tail_buffer[offset : offset + self.block_size])
            if self.blocksdb.has_block(md5):
                if offset - self.end_of_last_hit > 0:
                    self.__enter_state(ORIGINAL, self.end_of_last_hit)
                self.__enter_state(MATCH, offset)
                self.__add_hit(offset, md5)
                self.end_of_last_hit = offset + self.block_size
            #else:
            #    print "False hit at", offset

        if self.end_of_last_hit < (self.feed_byte_count - self.block_size):
            self.__enter_state(ORIGINAL, self.end_of_last_hit)
        # Guaranteed original data from self.end_of_last_hit to
        # (self.feed_byte_count - window_size). (The next byte that
        # arrives could potentially cause a block hit
        # [self.feed_byte_count - window_size, self.feed_byte_count])

    def close(self):
        assert not self.closed
        self.closed = True
        if self.end_of_last_hit != self.feed_byte_count:
            self.__enter_state(ORIGINAL, self.end_of_last_hit)
        self.__enter_state(END, self.feed_byte_count)
        if list(self.seqfinder.get_matches()):
            self.__finish_sequence()
        self.__add_original(self.end_of_last_hit, len(self.tail_buffer))
        original_size = len(self.tail_buffer)
        assert original_size == self.feed_byte_count
        recipe_size = 0
        for a in self.addresses:
            recipe_size += a.size * a.repeat
        #print_recipe(self.get_recipe())
        assert original_size == recipe_size
        assert self.md5summer.hexdigest() == self.md5summer_reconstruct.hexdigest()
        del self.rs

    def modify_piece(self, index, blob, offset):
        assert self.closed
        addresses = [a for a in self.addresses if a.piece_index == index]
        assert len(addresses) == 1
        p = addresses[0]
        p.blob = blob
        p.blob_offset = offset

    def get_recipe(self):
        assert self.closed
        pieces = []
        for address in self.addresses:
            assert address.size > 0
            pieces.append({"source": address.blob, 
                           "offset": address.blob_offset, 
                           "size": address.size, 
                           "original": address.original,
                           "repeat": address.repeat})
        result = {"method": "concat",
                  "md5sum": self.md5summer.hexdigest(),
                  "size": len(self.tail_buffer),
                  "pieces": pieces}
        #print_recipe(result)
        return result

    def __add_original(self, start_offset, end_offset):
        assert 0 <= start_offset <= end_offset
        if start_offset == end_offset:
            return
        assert start_offset < end_offset
        self.original_piece_handler.init_piece(self.original_piece_count)
        for block in self.tail_buffer.get_blocks(start_offset, end_offset - start_offset):
            self.original_piece_handler.add_piece_data(self.original_piece_count, block)
            self.md5summer_reconstruct.update(block)
        #print "Add original '%s'" % block
        self.original_piece_handler.end_piece(self.original_piece_count)
        #print "Found original data at %s+%s" % ( start_offset, end_offset - start_offset)
        self.__add_address(Struct(blob = None, piece_index = self.original_piece_count, blob_offset = start_offset, size = end_offset - start_offset, original = True, repeat = 1))
        #self.reconstructed_file.write(self.tail_buffer[start_offset:end_offset])
        self.original_piece_count += 1

    def __add_hit(self, offset, md5):
        """'Offset' is the point in the input stream where the match
        begins. The match is always as long as the current block
        size."""
        assert offset >= self.end_of_last_hit
        hit_size = self.block_size

        blob, blob_offset = self.blocksdb.get_block_locations(md5, limit=1).next()

        #if match_length: print "Block hit was expanded to %s+%s" % ( offset, hit_size)
        # There is original data from the end of the last true
        # hit, up to the start of this true hit.
        match_gap = self.end_of_last_hit != offset
        blob_gap = not self.seqfinder.can_add(md5)
        if match_gap or blob_gap:            
            #if match_gap: print "Gap in match at (before) position", self.end_of_last_hit
            #if blob_gap: print "Gap in blobs at (before) position", self.end_of_last_hit
            # There's a region of original data between this match and
            # the last one

            # Finish processing any deduplicated sequence
            if list(self.seqfinder.get_matches()):
                self.__finish_sequence()

            #print "Adding original from add_hit", self.end_of_last_hit, offset
            self.__add_original(self.end_of_last_hit, offset)

            # This is no longer a continous match - reset the sequence finder 
            self.seqfinder = self.blocksdb.get_sequence_finder()

        self.seqfinder.add_block(md5)
        #self.reconstructed_file.write(self.blob_source.get_blob_reader(blob, offset=blob_offset, size = hit_size).read())

    def __finish_sequence(self):
        blob, blob_offset, match_length = self.seqfinder.get_matches().next()
        assert blob_offset >= 0
        assert match_length > 0

        # TODO: remove/fix before release. Cannot read() an unknown amount of data.
        matched_data = self.blob_source.get_blob_reader(blob, offset=blob_offset, size = match_length).read()
        self.md5summer_reconstruct.update(matched_data)

        #print "Adding matched data '%s'" % matched_data

        self.__add_address(Struct(blob = blob, piece_index = None, blob_offset = blob_offset, size = match_length, original = False, repeat = 1))




    def __add_address(self, new_address):
        if not self.addresses:
            self.addresses.append(new_address)
        else:
            prev_address = self.addresses[-1]
            if prev_address.blob == new_address.blob and \
                    prev_address.blob_offset == new_address.blob_offset and \
                    prev_address.size == new_address.size:
                assert prev_address.piece_index == None and new_address.piece_index == None
                assert prev_address.original == False and new_address.original == False
                prev_address.repeat = prev_address.repeat + 1
            else:
                self.addresses.append(new_address)
                #print "Adding (appended)", new_address
                    
            
def print_recipe(recipe):
    print "Md5sum:", recipe["md5sum"]
    print "Size  :", recipe["size"]
    print "Method:", recipe['method']
    print "Pieces:", len(recipe['pieces'])
    pos = 0
    dedup_size = 0
    for p in recipe['pieces']:
        print "  Source blob  :", p['source'] if p['source'] else "SELF"
        print "  Source offset:", p['offset']
        print "  Size         :", p['size']
        print "  Position     : %s - %s" % (hex(pos), hex(pos + p['size'] * p.get('repeat', 1)))
        print "  Repeat       :", p.get('repeat', "(1)")
        print "  ---------------"
        pos += p['size']
        if p['source'] == None: # Count the parts we couldn't find elsewhere
            dedup_size += p['size']
    try:
        print "Dedup removed %s%% of original size" % round((100.0 * (1.0 - float(dedup_size) / recipe["size"])), 1)
    except ZeroDivisionError:
        print "Zero size recipe"
        pass

def recepify(front, filename, local_blob_dir = None):
    WINDOW_SIZE = front.get_dedup_block_size()
    blob_source = UniformBlobGetter(front.repo, local_blob_dir)
    original_size = os.path.getsize(filename)
    if original_size == 0:
        # Zero length files don't deduplicate well...
        return None

    finder = RecipeFinder(front.repo, WINDOW_SIZE, front.get_all_rolling(), blob_source)
    f = safe_open(filename, "rb")
    for block in file_reader(f, blocksize = WINDOW_SIZE):
        finder.feed(block)
    finder.close()
    f.close()

    recipe = finder.get_recipe()

    return recipe

def benchmark():
    import time
    b = BlockChecksum(2**16)
    data = "x" * 12345
    t0 = time.time()
    for n in range(0,10000):
        b.feed_string(data)
        b.harvest()
    print time.time() - t0

#res=cProfile.run('main()', "prof.txt")
#import pstats
#p = pstats.Stats('prof.txt')
#p.sort_stats('cum').print_stats(20)
#sys.exit(res)

def main():
    import cProfile, pstats
    res=cProfile.run('benchmark()', "deduplication_prof.txt")
    p = pstats.Stats('deduplication_prof.txt')
    p.sort_stats('cum').print_stats(20)

#main()
#benchmark()

