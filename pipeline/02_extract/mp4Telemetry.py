"""Read unfragmented MP4 metadata tracks without decoding video.

Only the ssmd/gpmd layouts used by this study are supported. Unsupported timing
edits are rejected rather than silently treated as synchronized samples.
"""
import struct


def boxes(data):
    position = 0
    while position + 8 <= len(data):
        size, tag = struct.unpack_from('>I4s', data, position)
        header = 8
        if size == 1:
            size = struct.unpack_from('>Q', data, position + 8)[0]
            header = 16
        if size == 0:
            size = len(data) - position
        if size < header or position + size > len(data):
            raise ValueError(f'Invalid MP4 box {tag!r}')
        yield tag, data[position + header:position + size]
        position += size
    if position != len(data):
        raise ValueError('Trailing bytes in MP4 container')


def child(data, tag):
    return next((body for key, body in boxes(data) if key == tag), None)


def moov(path):
    with path.open('rb') as handle:
        length = path.stat().st_size
        while handle.tell() + 8 <= length:
            position = handle.tell()
            size, tag = struct.unpack('>I4s', handle.read(8))
            header = 8
            if size == 1:
                size = struct.unpack('>Q', handle.read(8))[0]
                header = 16
            if size == 0:
                size = length - position
            if size < header or position + size > length:
                raise ValueError('Invalid top-level MP4 box')
            if tag == b'moov':
                return handle.read(size - header)
            handle.seek(position + size)
    raise ValueError('No moov box; fragmented/unsupported MP4')


def metadataTracks(path):
    """Return decode times and payload positions. Reject edits on sensor tracks."""
    result = []
    for tag, track in boxes(moov(path)):
        if tag != b'trak':
            continue
        media = child(track, b'mdia')
        table = child(child(media, b'minf'), b'stbl')
        codec = child(table, b'stsd')[12:16]
        if codec not in (b'ssmd', b'gpmd'):
            continue
        if child(track, b'edts') is not None or child(table, b'ctts') is not None:
            raise ValueError('Sensor edit/composition timeline needs explicit review')
        header = child(media, b'mdhd')
        timescale = struct.unpack_from('>I', header, 20 if header[0] else 12)[0]
        if not timescale:
            raise ValueError('Zero media timescale')
        sizesBox = child(table, b'stsz')
        size, count = struct.unpack_from('>II', sizesBox, 4)
        sizes = [size] * count if size else list(struct.unpack_from(f'>{count}I', sizesBox, 12))
        # ROVE also stores a one-frame JPEG thumbnail under the ssmd codec.
        if codec == b'ssmd' and set(sizes) not in ({12}, {32}):
            continue
        timing = child(table, b'stts')
        entryCount = struct.unpack_from('>I', timing, 4)[0]
        durations = []
        for i in range(entryCount):
            repetitions, delta = struct.unpack_from('>II', timing, 8 + i * 8)
            durations.extend([delta] * repetitions)
        if len(durations) != count or any(d <= 0 for d in durations):
            raise ValueError('Invalid sensor sample durations')
        starts, elapsed = [], 0
        for delta in durations:
            starts.append(elapsed / timescale)
            elapsed += delta
        chunkBox = child(table, b'stco')
        longOffsets = chunkBox is None
        if longOffsets:
            chunkBox = child(table, b'co64')
        chunkCount = struct.unpack_from('>I', chunkBox, 4)[0]
        offsets = struct.unpack_from(f'>{chunkCount}{"Q" if longOffsets else "I"}', chunkBox, 8)
        mapping = child(table, b'stsc')
        entries = [struct.unpack_from('>III', mapping, 8 + i * 12)
                   for i in range(struct.unpack_from('>I', mapping, 4)[0])]
        positions, entry, sample = [], 0, 0
        for chunkIndex, offset in enumerate(offsets, 1):
            while entry + 1 < len(entries) and chunkIndex >= entries[entry + 1][0]:
                entry += 1
            for _ in range(entries[entry][1]):
                if sample >= count:
                    raise ValueError('Chunk map contains excess samples')
                positions.append(offset)
                offset += sizes[sample]
                sample += 1
        if sample != count or any(p + n > path.stat().st_size for p, n in zip(positions, sizes)):
            raise ValueError('Invalid payload bounds/counts')
        result.append(dict(codec=codec.decode(), times=starts, positions=positions,
                           sizes=sizes, durationSec=elapsed / timescale))
    return result


def klv(data):
    position = 0
    while position + 8 <= len(data):
        key, kind, size, count = struct.unpack_from('>4sBBH', data, position)
        if key == b'\0\0\0\0':
            break
        length = size * count
        if position + 8 + length > len(data):
            raise ValueError('Truncated GPMF data')
        yield key.decode('ascii'), kind, size, count, data[position + 8:position + 8 + length]
        position += 8 + (length + 3) // 4 * 4


def streams(data):
    for key, kind, size, count, payload in klv(data):
        if key == 'STRM':
            yield {k: (t, s, n, p) for k, t, s, n, p in klv(payload)}
        elif kind == 0:
            yield from streams(payload)


def number(field):
    kind, size, count, payload = field
    formats = {ord('s'): 'h', ord('S'): 'H', ord('l'): 'i', ord('L'): 'I', ord('J'): 'Q'}
    if kind not in formats or count != 1:
        raise ValueError('Unsupported scalar GPMF field')
    return struct.unpack('>' + formats[kind], payload)[0]
