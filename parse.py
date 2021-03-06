import os
import sys
import re
import pandas as pd
from distutils.version import StrictVersion
from collections import Counter
import time
import traceback
from crawl_data import SPECIES, BGS, GODS, BRANCHES
import crawl_data

# Inaccurately named now
STRICT_BG_CHECK = 1
ROW_LIMIT = 0

# Running this over gbs of logs takes a while. It kind of sucks to lose all progress
# after 20 minutes because of a trivial error. If this is true, just print unhandled
# exception traces and continue
SOFT_ERRORS = 1

FLUSH_EVERY = 300000

MAX_UNSUPPORTED_VERSION = StrictVersion('0.9')

class VersionException(Exception):
    pass

class OldVersionException(Exception):
    pass

class ChunkExhaustionException(Exception):
    pass

class MinigameException(Exception):
    pass

# I'm going to ignore the corresponding vampire lines (you were thirsty, very thirsty, etc.)
HUNGER_LINES = ['you were not hungry.', 'you were completely stuffed.', 'you were hungry.', 
    'you were full.', 'you were very hungry.', 'you were near starving.', 'you were very full.',
    'you were starving.',]
HUNGER_LINES = {line: line[len('you were '):-1] for line in HUNGER_LINES}

def flush(rows, store, i):
    df = framify(rows, i)
    # Appending is a huge pain in the ass, because it'll complain about
    # any difference in columns or even if you try to insert a categorical
    # value not seen yet. So just save separate chunks and sew them 
    # back together later. Bleh.
    store.put('chunk{}'.format(i), df, format='table')
    store['columns'] = df.columns

def framify(rows, chunk_idx):
    """Turn a list of rows in dict representations into a pandas dataframe.
    """
    frame = pd.DataFrame(rows) #, index=range(FLUSH_EVERY*chunk_idx, FLUSH_EVERY))
    frame['nrunes'].fillna(0, inplace=1)
    for col in frame.columns:
        boolean_prefixes = ['rune_', 'visited_', 'saw_']
        if any(col.startswith(pre) for pre in boolean_prefixes):
            frame[col].fillna(False, inplace=1)
        elif col.startswith('skill_'):
            frame[col].fillna(0.0, inplace=1)
        elif col in ['bg', 'god', 'species', 'wheredied', 'howdied', 'hunger']:
            frame[col] = frame[col].astype('category')

    versions = [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18, 0.19, 0.20]
    frame['version'] = frame['version'].astype("category", categories=versions,
            ordered=True)
    lvls = range(1, 28)
    frame['level'] = frame['level'].astype("category", categories=lvls, ordered=True)

    non_null_cols = ['bg', 'god', 'level', 'nrunes', 'species', 'time', 'turns',
            'version', 'won']
    for col in non_null_cols:
        if frame[col].count() != len(frame):
            print 'Got unexpected null values in column {}'.format(col)

    # Object columns are bad news in terms of memory usage.
    for col in frame.columns:
        if frame[col].dtype.name == 'object':
            print "~~~ WARNING ~~~: Column {} has object type. Are you sure " \
                    "you wouldn't rather use a category?".format(col)

    return frame


class Morgue(object):
    # Keep track of seen bots so we can save them to a file at the end for later use
    bots = set()
    def __init__(self, f):
        self.f = f
        # The core morgue data structure. Used for simple scalar columns.
        self.m = {}
        self.skills = {}
        try:
            self.parse()
        except Exception as e:
            e.fname = self.f.name
            e.version = self.m.get('version', 'UNK')
            # Unfortunately, it seems like the original line number gets lost
            # when reraising. This seems like a hack. Is there no better way?
            e.trace = traceback.format_exc(e)
            raise e

    def setcol(self, col, value):
        # I thought I needed to inject some behaviour here, but turns out I don't.
        self.m[col] = value

    @staticmethod
    def normalize_wheredied(wd):
        if wd in crawl_data.CANON_WD:
            return wd
        if wd == 'tomb of the ancients':
            return 'tomb'
        if wd == "spider's nest":
            return "spider nest"
        if wd.startswith('level') and 'ziggurat' in wd:
            return 'ziggurat'
        if wd == "pandemonium (cerebov's castle)":
            return 'pandemonium'
        if wd == 'sewers':
            return 'sewer'
        if wd.startswith('ecumenical temple'):
            # Sometimes see "ecumenical temple (autumnal temple)". No idea.
            return 'ecumenical temple'
        if wd in crawl_data.WIZLABS:
            return "wizard's laboratory"
        return 'other'

    def parse_summary(self, lines):
        """Parse the section at the top, after the version line.
        Frodo the Vexing (level X)
        began as a merfolk haberdasher
        etc."""
        match = re.match('(?P<score>\d+) (?P<name>[^ ]*) .*\(level (?P<level>\d+),', lines[0])
        assert match, "Couldn't find score/name/level in line: " + lines[0] 
        d = match.groupdict()
        # Save player name for future logging
        self.name = d['name']
        self.setcol('score', int(d['score']))
        self.setcol('level', int(d['level']))

        match = re.match('began as an? (.*) on', lines[1])
        assert match, lines[1]
        combo = match.group(1)
        parts = combo.split()
        if len(parts) == 2:
            sp, bg = parts
        elif parts[0] in SPECIES:
            sp = parts[0]
            bg = ' '.join(parts[1:])
        else:
            sp = ' '.join(parts[:2])
            bg = ' '.join(parts[2:])
        try:
            assert sp in SPECIES, 'Unrecognized species: {}'.format(sp)
            assert bg in BGS, 'Unrecognized background: {}'.format(bg)
        except AssertionError as e:
            if STRICT_BG_CHECK:
                raise e
            else:
                print e.message
        # Was renamed in 0.10
        if sp == 'kenku':
            sp = 'tengu'
        self.setcol('species', sp)
        self.setcol('bg', bg)

        # Seems to be some inconsistency in which lines appear where in this
        # blob. May get a line about god worshipped, if there was one, 
        # inconsistent info on timing/duration, and possibly some hard wrapping
        # We want to muddle our way through this and eventually find out whether
        # this character won or died, and where they died
        i = 2
        won = None
        wheredied = None
        howdied = None
        while i < len(lines):
            line = lines[i]
            i += 1
            if line.startswith('escaped with the orb'):
                won = True
                break
            if (line == 'safely got out of the dungeon.'
                    or line == 'got out of the dungeon alive.'
                    or line.startswith('quit the game')
                    or line == 'got out of the dungeon'
                ):
                howdied = 'quit'
                won = False
                break
            # TODO :What is 'burnt to a crisp'?
            # Man, there is a lot of variety in these messages. :(
            # TODO: Killed from afar one not strictly correct. Could rarely be god wrath
            # ('killed from afar by the fury of makhleb')
            monster_death_prefixes = ['slain by', 'mangled by', 'shot with',
                    'killed from afar', 'hit by', 'demolished by', 'annihilated by',
                    # This one mostly appears in the context of 
                    # 'Killed by X\n... invoked by Y' (smiting, pain)
                    # Can also prefix miscasts (checked earlier), and some
                    # other weird, rare stuff (killed by angry trees, killed by a
                    # spatial distortion.
                    'killed by',
                    # This one is kinda weird. Maybe the result of too much draining
                    # at a low level, exhausting all xp or something? v rare
                    'was drained of all life',
                    'drained of all life',
                    'blown up by', 'splashed by', 'splashed with', 'drowned by',
                    # How the hell?
                    'thrown by',
                    # Dying to a monster's poison is maybe not really worth
                    # distinguishing from the general case of dying to a monster
                    'succumbed to',
                    'incinerated by',
                    'impaled on', 'headbutted by', 'rolled over by'
            ]
            if line.startswith('killed by miscasting'):
                howdied = 'miscast'
            elif line == 'succumbed to poison (a potion of poison)':
                howdied = 'suicide'
            elif line.startswith('distortion unwield') or line.startswith('killed by distortion unwield'):
                howdied = 'suicide'
            # Above checks need to happen first, since they're subsumed by more
            # generic monster death prefixes
            elif (any(line.startswith(pre) for pre in monster_death_prefixes)
                    ):
                howdied = 'monster'
            elif 'themsel' in line:
                # This has the happy effect of setting the correct cause of death
                # for "Killed by an exploding spore\n...Set off by themselves"
                howdied = 'suicide'
            elif line.startswith('rotted away'):
                howdied = 'rot'
            elif line.startswith('engulfed by'):
                # Hacky approximation. If it originated from a monster, it'll
                # be something like "Engulfed by a death drake's foul pestilence",
                # or "an ice statue's freezing vapour", otherwise it'll just be
                # something like "engulfed by a cloud of flame"
                howdied = 'monster' if "'s" in line else 'cloud'
            elif line.startswith('starved to death'):
                howdied = 'starved'
            elif line == 'asphyxiated':
                howdied = 'asphyxiated'
            elif line == 'drowned' or line == 'took a swim in molten lava':
                howdied = 'drowned'
            elif (line == 'forgot to exist' or line == 'slipped on a banana peel'
                    or line == 'forgot to breathe'):
                howdied = 'statdeath'


            r = ('\.\.\. (in|on level (?P<lvl>\d+) of) ((the|a|an) )?'
                    +'(?P<branch>.*?)( on .*)?.$')
            m = re.match(r, line)
            if m:
                wheredied = m.group('branch')
                won = False
                break

        assert won is not None, "Couldn't figure out whether they won: {}".format(lines)
        # This isn't a dealbreaker. We should just record a row with nan for howdied.
        if not (won or howdied):
            print "Warning: Couldn't determine cause of death for: {}".format(lines)
        self.setcol('won', won)
        self.setcol('wheredied', wheredied and self.normalize_wheredied(wheredied))
        self.setcol('howdied', howdied)

        timeline = lines[-1]
        match = re.match('the game lasted (.*) \((\d+) turns?\)', timeline)
        assert match, 'Unexpected line: {}'.format(timeline)
        timestr, turns = match.groups()
        parts = timestr.strip().split()
        # '1day 11:22:33', or '1 day 11:22:33' or '11:22:33'
        assert 1 <= len(parts) <= 3, timestr
        if len(parts) == 2:
            digits = [c for c in parts[0] if c.isdigit()]
            days = int(''.join(digits))
        elif len(parts) == 3:
            days = int(parts[0])
        else:
            days = 0
        hrs, mins, secs = map(int, parts[-1].split(':'))
        total_seconds = secs + 60*mins + 60*60*hrs + 24*60*60*days
        self.setcol('time', total_seconds)
        self.setcol('turns', int(turns))


    def parse(self):
        l = self.next_line()
        if not l.startswith('dungeon crawl stone soup version '):
            # Won't match in case of sprint, zot defense, etc.
            raise MinigameException()
        vstring = l.split()[5]
        # Simplifying(?)
        match = re.match('\d\.\d+', vstring)
        if not match:
            raise VersionException(vstring)
        v = match.group()
        if StrictVersion(v) <= MAX_UNSUPPORTED_VERSION:
            raise OldVersionException()
        self.setcol('version', float(v))

        self.parse_summary(self.next_chunk())

        self.next_chunk()
        lines = self.next_chunk()
        godline = lines[1]
        parts = godline.split()
        try:
            gindex = parts.index('god:') + 1
        # Weird spacing issues with big stats?
        except ValueError:
            for i, part in enumerate(parts):
                if 'god:' in part:
                    gindex = i + 1
                    break
            else:
                raise Exception('Weird line: ' + godline)
        if gindex == len(parts):
            # there is no god
            god = 'none'
        # Xom formatting is a bit wonky
        elif parts[0] in ('xom', '*xom'):
            god = 'xom'
        elif gindex == len(parts) - 1: # Special case for gozag, who doesn't have piety
            god = parts[gindex]
        else:
            god = ' '.join(parts[gindex:-1])
            assert god, godline
        # Sometimes we'll get a string like '*Zin', meaning worshipping Zin but under penance
        if god[0] == '*':
            god = ' '.join(parts[gindex:])[1:]
        # Apparently some inconsistency in case for TSO
        god = god.lower()
        assert god == 'none' or god in GODS, 'Unrecognized god: {}'.format(god)
        self.setcol('god', god)

        percent = self.next_chunk()

        lines = self.next_chunk()
        statuses = lines[0]
        # TODO: handle statuses
        # TODO: introduces columns with mostly missing values. Problem? Should look into sparse data structures.
        i = 0
        while i < len(lines):
            line = lines[i]
            i += 1
            if line.startswith('}:'):
                # wrapping. This is gonna be a recurring problem.
                if line.endswith(','):
                    line += ' ' + lines[i]
                    i += 1
                match = re.match(r'}: (\d+)/15 runes: (.*)$', line)
                n, runestr = match.groups()
                self.setcol('nrunes', int(n))
                runes = [rune.strip() for rune in runestr.split(',')]
                for rune in runes:
                    assert rune, 'Got into a bad rune situation: {}'.format(lines)
                    self.setcol('rune_{}'.format(rune), True)
                break

        # You were in the dungeon, you worshipped X, you were hungry, etc.
        while 1:
            chunk = self.next_chunk()
            if chunk[0].startswith('you visited'):
                break
            # Pretty sure these usually come before 'you visited', but should be robust to different orders
            for line in chunk:
                if line in HUNGER_LINES:
                    self.setcol('hunger', HUNGER_LINES[line])

        visited = chunk
        assert visited[0].startswith('you visited'), 'Bad visit chunk. First line: {}'.format(visited[0])
        for line in visited:
            # Maybe this should have been done in next_chunk. Probably too late now.
            line = line.strip().lower()
            prefix = 'you also visited: '
            if not line.startswith(prefix):
                continue
            # Skip the last char, which is a period.
            portalstr = line[len(prefix):-1].replace(' and', ',')
            portals = portalstr.split(', ')
            for portal in portals:
                try:
                    # Sometimes get stuff like 'labyrinth (2 times)'
                    # TODO: this is probably information I should record for the
                    # purposes of some analyses, though I think it's a pretty 
                    # rare phenomenon (is it even possible to get 2 of the same 
                    # portal vault in one game in the current version?)
                    paren = portal.index('(')
                    portal = portal[:paren]
                except ValueError:
                    pass
                self.setcol('visited_'+portal.strip(), True)


        # Originally relied on fixed ordering of chunks, but it turns out there's
        # some variation between versions, so just scan for known chunk headers
        # anywhere
        header_to_method = {
                'skills:': self.parse_skills,
                'branches:': self.parse_branches,
                'notes': self.parse_notes,
        }
        sought = header_to_method.keys()
        while sought:
            try:
                chunk = self.next_chunk()
            except ChunkExhaustionException as e:
                e.message = 'Traversed whole file without finding headers: {}'.format(sought)
                raise e
            header = chunk[0]
            if header in sought:
                header_to_method[header](chunk)
                sought.remove(header)
        
        
    def parse_skills(self, chunk):
        # Parse skill levels
        # It seems like a nice idea to have some kind of series/dataframe nested
        # inside the main one for stuff like runes, skills, spells, etc. (if only
        # for the sake of tidiness and not having to do ersatz namespacing), but 
        # it seems like that's something that's not really well-supported by pandas?
        for skill_line in chunk[1:]:
            match = re.search('level (\d+\.?\d?)', skill_line)
            assert match, skill_line
            lvl = float(match.group(1))
            skill_start = 2
            parts = skill_line.split()
            while not parts[skill_start].isalpha():
                skill_start += 1
            skill_name = ' '.join(parts[skill_start:])
            assert skill_name, skill_line
            self.setcol('skill_'+skill_name, lvl)

    def parse_branches(self, chunk):
        blob = ' '.join(chunk[1:])
        tokens = blob.split()
        i = 0
        # Looking around for substrings like 'Temple (1/1) D:7'
        while i < len(tokens):
            w = tokens[i]
            i += 1
            if w not in BRANCHES:
                continue
            branch = w
            w = tokens[i]
            i += 1
            # I think this indicates the player has at least seen the entrance
            # I think (0/X) means they haven't entered it. If they've been to
            # the range of levels where it should appear, but haven't seen the
            # entrance, they get something like 'Vaults: D:13-D14'
            if w[0] == '(':
                self.setcol('saw_'+branch, True)


    def parse_notes(self, chunk):
        # elliptic's qw bot (which I think is the only one really in use, or 
        # at least the most popular), leaves some telltale marks in the notes
        # section. 
        # Can probably get away with just checking first 10 notes or so (in fact,
        # I'm pretty sure it's guaranteed to put a note like '0 ||| counter = 303'
        # in the third note every time)
        bot = False
        for noteline in chunk[3:13]:
            if re.search(' \d+ \|\|\| ', noteline):
                bot = True
                self.bots.add(self.name)
                break
        self.setcol('bot', bot)
        if not bot and self.name in self.bots:
            print "WARNING: {} was in list of known bots, but seemed not to be botting this game: {}".format(self.f.name)
            

    def next_chunk(self):
        """Return the next "chunk" in this morgue file - i.e. the next non-blank
        line and all lines after it until the next blank line - as a list of strings 
        (which we strip and lowercase before returning)"""
        chunk = []
        while 1:
            line = self.f.readline()
            if line == '':
                if chunk:
                    return chunk
                else:
                    raise ChunkExhaustionException()
            if line == '\n':
                # The last call read until it encountered a blank line. But
                # sometimes there's more than one consecutive blank line, which
                # we need to power through before getting to the good stuff
                if len(chunk) == 0:
                    continue
                else:
                    break
            chunk.append(line.strip().lower())
        return chunk

    def next_line(self):
        """Return the next non-blank line, stripped and lowercased."""
        while 1:
            line = self.f.readline()
            if line == '':
                raise ChunkExhaustionException()
            elif line == '\n':
                continue
            return line.strip().lower()

if __name__ == '__main__':
    t0 = time.time()
    morgue_dir = sys.argv[1]
    rows = []
    skips = Counter()
    niters = 0
    chunk_index = 0
    done = False
    minigame_files = []
    
    SAVE = 1
    if SAVE:
        fname = 'morgue.h5'
        assert not os.path.exists(fname), "{} already exists".format(fname)
        store = pd.HDFStore(fname)

    
    for parent, _, fnames in os.walk(morgue_dir):
        for fname in fnames:
            if not fname.endswith('.txt') or not fname.startswith('morgue'):
                continue
            with open(os.path.join(parent, fname)) as f:
                try:
                    morg = Morgue(f)
                except VersionException as ve:
                    skips['vstring'] += 1
                    continue
                except MinigameException as me:
                    skips['minigame'] += 1
                    minigame_files.append(f.name)
                    continue
                except OldVersionException as ove:
                    skips['old'] += 1
                    continue
                except Exception as e:
                    print "Unhandled {} in file {}. Version={}".format(
                            e.__class__.__name__, e.fname, e.version)
                    if e.message:
                        print e.message
                    # Turning this off for now, cause it's a little verbose
                    print "Original trace: {}".format(e.trace)
                    if not SOFT_ERRORS:
                        raise e
                    skips['unexpected'] += 1
                    continue
                rows.append(morg.m)
                niters += 1
                if (niters % 1000) == 0:
                    print 'i={} '.format(niters),

                if (ROW_LIMIT and niters >= ROW_LIMIT):
                    done = True
                    break

                if (niters % FLUSH_EVERY) == 0 and SAVE:
                    print "Flushing {} rows to hdfstore".format(FLUSH_EVERY)
                    flush(rows, store, chunk_index)
                    chunk_index += 1
                    rows = []

        if done:
            break

    if rows and SAVE:
        flush(rows, store, chunk_index)
        del rows

    print "Finished after {:.0f} seconds".format(time.time()-t0)

    print "Skips: {}".format(skips)

    with open('known_bots.txt', 'w') as f:
        f.write('\n'.join(list(Morgue.bots)) + '\n')

    if minigame_files:
        fname = 'minigames.txt'
        print "Writing {} minigame morgue paths to {}".format(len(minigame_files), fname)
        with open(fname, 'w') as f:
            f.write('\n'.join(minigame_files) + '\n')
