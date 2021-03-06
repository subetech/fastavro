# cython: auto_cpdef=True

"""Python code for reading AVRO files"""

# This code is a modified version of the code at
# http://svn.apache.org/viewvc/avro/trunk/lang/py/src/avro/ which is under
# Apache 2.0 license (http://www.apache.org/licenses/LICENSE-2.0)

import json
from struct import unpack, error as StructError
from zlib import decompress
import datetime
from decimal import localcontext, Decimal

try:
    from fastavro._six import MemoryIO, xrange, btou, utob, iteritems,\
        is_str, str2ints, fstint
    from fastavro._schema import (
        extract_record_type, acquaint_schema, populate_schema_defs,
        extract_logical_type
    )
except ImportError:
    from fastavro.six import MemoryIO, xrange, btou, utob, iteritems, \
        is_str, str2ints, fstint
    from fastavro.schema import (
        extract_record_type, acquaint_schema, populate_schema_defs,
        extract_logical_type
    )

VERSION = 1
MAGIC = b'Obj' + utob(chr(VERSION))
SYNC_SIZE = 16
HEADER_SCHEMA = {
    'type': 'record',
    'name': 'org.apache.avro.file.Header',
    'fields': [
        {
            'name': 'magic',
            'type': {'type': 'fixed', 'name': 'magic', 'size': len(MAGIC)},
        },
        {
            'name': 'meta',
            'type': {'type': 'map', 'values': 'bytes'}
        },
        {
            'name': 'sync',
            'type': {'type': 'fixed', 'name': 'sync', 'size': SYNC_SIZE}
        },
    ]
}
MASK = 0xFF
AVRO_TYPES = set([
    'boolean',
    'bytes',
    'double',
    'float',
    'int',
    'long',
    'null',
    'string',
    'fixed',
    'enum',
    'record',
    'error',
    'array',
    'map',
    'union',
    'request',
    'error_union'
])


class SchemaResolutionError(Exception):
    pass


def match_types(writer_type, reader_type):
    if isinstance(writer_type, list) or isinstance(reader_type, list):
        return True
    if writer_type == reader_type:
        return True
    # promotion cases
    elif writer_type == 'int' and reader_type in ['long', 'float', 'double']:
        return True
    elif writer_type == 'long' and reader_type in ['float', 'double']:
        return True
    elif writer_type == 'float' and reader_type == 'double':
        return True
    return False


def match_schemas(w_schema, r_schema):
    error_msg = 'Schema mismatch: %s is not %s' % (w_schema, r_schema)
    if isinstance(w_schema, list):
        # If the writer is a union, checks will happen in read_union after the
        # correct schema is known
        return True
    elif isinstance(r_schema, list):
        # If the reader is a union, ensure one of the new schemas is the same
        # as the writer
        for schema in r_schema:
            if match_types(w_schema, schema):
                return True
        else:
            raise SchemaResolutionError(error_msg)
    else:
        # Check for dicts as primitive types are just strings
        if isinstance(w_schema, dict):
            w_type = w_schema['type']
        else:
            w_type = w_schema
        if isinstance(r_schema, dict):
            r_type = r_schema['type']
        else:
            r_type = r_schema

        if w_type == r_type == 'map':
            if match_types(w_schema['values'], r_schema['values']):
                return True
        elif w_type == r_type == 'array':
            if match_types(w_schema['items'], r_schema['items']):
                return True
        elif match_types(w_type, r_type):
            return True
        raise SchemaResolutionError(error_msg)


def read_null(fo, writer_schema=None, reader_schema=None):
    """null is written as zero bytes."""
    return None


def read_boolean(fo, writer_schema=None, reader_schema=None):
    """A boolean is written as a single byte whose value is either 0 (false) or
    1 (true).
    """

    # technically 0x01 == true and 0x00 == false, but many languages will cast
    # anything other than 0 to True and only 0 to False
    return unpack('B', fo.read(1))[0] != 0


def parse_timestamp(data, resolution):
    return datetime.datetime.fromtimestamp(data / resolution).replace(
        microsecond=data % resolution)


def read_timestamp_millis(data, writer_schema=None, reader_schema=None):
    return parse_timestamp(data, 1000)


def read_timestamp_micros(data, writer_schema=None, reader_schema=None):
    return parse_timestamp(data, 1000000)


def read_date(data, writer_schema=None, reader_schema=None):
    return datetime.date.fromordinal(data)


def read_bytes_decimal(data, writer_schema=None, reader_schema=None):
    """
    Decimal is encoded as fixed. Fixed instances are encoded using the
    number of bytes declared in the schema.
    based on https://github.com/apache/avro/pull/82/
    """
    scale = writer_schema['scale']
    precision = writer_schema['precision']

    size = len(data)

    datum_byte = str2ints(data)

    unscaled_datum = 0
    msb = fstint(data)
    leftmost_bit = (msb >> 7) & 1
    if leftmost_bit == 1:
        modified_first_byte = datum_byte[0] ^ (1 << 7)
        datum_byte = [modified_first_byte] + datum_byte[1:]
        for offset in xrange(size):
            unscaled_datum <<= 8
            unscaled_datum += datum_byte[offset]
        unscaled_datum += pow(-2, (size * 8) - 1)
    else:
        for offset in xrange(size):
            unscaled_datum <<= 8
            unscaled_datum += (datum_byte[offset])

    with localcontext() as ctx:
        ctx.prec = precision
        scaled_datum = Decimal(unscaled_datum).scaleb(-scale)
    return scaled_datum


def read_long(fo, writer_schema=None, reader_schema=None):
    """int and long values are written using variable-length, zig-zag
    coding."""
    c = fo.read(1)

    # We do EOF checking only here, since most reader start here
    if not c:
        raise StopIteration

    b = ord(c)
    n = b & 0x7F
    shift = 7

    while (b & 0x80) != 0:
        b = ord(fo.read(1))
        n |= (b & 0x7F) << shift
        shift += 7

    return (n >> 1) ^ -(n & 1)


def read_float(fo, writer_schema=None, reader_schema=None):
    """A float is written as 4 bytes.

    The float is converted into a 32-bit integer using a method equivalent to
    Java's floatToIntBits and then encoded in little-endian format.
    """

    return unpack('<f', fo.read(4))[0]


def read_double(fo, writer_schema=None, reader_schema=None):
    """A double is written as 8 bytes.

    The double is converted into a 64-bit integer using a method equivalent to
    Java's doubleToLongBits and then encoded in little-endian format.
    """
    return unpack('<d', fo.read(8))[0]


def read_bytes(fo, writer_schema=None, reader_schema=None):
    """Bytes are encoded as a long followed by that many bytes of data."""
    size = read_long(fo)
    return fo.read(size)


def read_utf8(fo, writer_schema=None, reader_schema=None):
    """A string is encoded as a long followed by that many bytes of UTF-8
    encoded character data.
    """
    return btou(read_bytes(fo), 'utf-8')


def read_fixed(fo, writer_schema, reader_schema=None):
    """Fixed instances are encoded using the number of bytes declared in the
    schema."""
    return fo.read(writer_schema['size'])


def read_enum(fo, writer_schema, reader_schema=None):
    """An enum is encoded by a int, representing the zero-based position of the
    symbol in the schema.
    """
    index = read_long(fo)
    symbol = writer_schema['symbols'][index]
    if reader_schema and symbol not in reader_schema['symbols']:
        symlist = reader_schema['symbols']
        msg = '%s not found in reader symbol list %s' % (symbol, symlist)
        raise SchemaResolutionError(msg)
    return symbol


def read_array(fo, writer_schema, reader_schema=None):
    """Arrays are encoded as a series of blocks.

    Each block consists of a long count value, followed by that many array
    items.  A block with count zero indicates the end of the array.  Each item
    is encoded per the array's item schema.

    If a block's count is negative, then the count is followed immediately by a
    long block size, indicating the number of bytes in the block.  The actual
    count in this case is the absolute value of the count written.
    """
    if reader_schema:
        def item_reader(fo, w_schema, r_schema):
            return read_data(fo, w_schema['items'], r_schema['items'])
    else:
        def item_reader(fo, w_schema, _):
            return read_data(fo, w_schema['items'])

    read_items = []

    block_count = read_long(fo)

    while block_count != 0:
        if block_count < 0:
            block_count = -block_count
            # Read block size, unused
            read_long(fo)

        for i in xrange(block_count):
            read_items.append(item_reader(fo, writer_schema, reader_schema))
        block_count = read_long(fo)

    return read_items


def read_map(fo, writer_schema, reader_schema=None):
    """Maps are encoded as a series of blocks.

    Each block consists of a long count value, followed by that many key/value
    pairs.  A block with count zero indicates the end of the map.  Each item is
    encoded per the map's value schema.

    If a block's count is negative, then the count is followed immediately by a
    long block size, indicating the number of bytes in the block.  The actual
    count in this case is the absolute value of the count written.
    """
    if reader_schema:
        def item_reader(fo, w_schema, r_schema):
            return read_data(fo, w_schema['values'], r_schema['values'])
    else:
        def item_reader(fo, w_schema, _):
            return read_data(fo, w_schema['values'])

    read_items = {}
    block_count = read_long(fo)
    while block_count != 0:
        if block_count < 0:
            block_count = -block_count
            # Read block size, unused
            read_long(fo)

        for i in xrange(block_count):
            key = read_utf8(fo)
            read_items[key] = item_reader(fo, writer_schema, reader_schema)
        block_count = read_long(fo)

    return read_items


def read_union(fo, writer_schema, reader_schema=None):
    """A union is encoded by first writing a long value indicating the
    zero-based position within the union of the schema of its value.

    The value is then encoded per the indicated schema within the union.
    """
    # schema resolution
    index = read_long(fo)
    if reader_schema:
        # Handle case where the reader schema is just a single type (not union)
        if not isinstance(reader_schema, list):
            if match_types(writer_schema[index], reader_schema):
                return read_data(fo, writer_schema[index], reader_schema)
        else:
            for schema in reader_schema:
                if match_types(writer_schema[index], schema):
                    return read_data(fo, writer_schema[index], schema)
        msg = 'schema mismatch: %s not found in %s' % \
            (writer_schema, reader_schema)
        raise SchemaResolutionError(msg)
    else:
        return read_data(fo, writer_schema[index])


def read_record(fo, writer_schema, reader_schema=None):
    """A record is encoded by encoding the values of its fields in the order
    that they are declared. In other words, a record is encoded as just the
    concatenation of the encodings of its fields.  Field values are encoded per
    their schema.

    Schema Resolution:
     * the ordering of fields may be different: fields are matched by name.
     * schemas for fields with the same name in both records are resolved
         recursively.
     * if the writer's record contains a field with a name not present in the
         reader's record, the writer's value for that field is ignored.
     * if the reader's record schema has a field that contains a default value,
         and writer's schema does not have a field with the same name, then the
         reader should use the default value from its field.
     * if the reader's record schema has a field with no default value, and
         writer's schema does not have a field with the same name, then the
         field's value is unset.
    """
    record = {}
    if reader_schema is None:
        for field in writer_schema['fields']:
            record[field['name']] = read_data(fo, field['type'])
    else:
        readers_field_dict = \
            dict((f['name'], f) for f in reader_schema['fields'])
        for field in writer_schema['fields']:
            readers_field = readers_field_dict.get(field['name'])
            if readers_field:
                record[field['name']] = read_data(fo,
                                                  field['type'],
                                                  readers_field['type'])
            else:
                # should implement skip
                read_data(fo, field['type'], field['type'])

        # fill in default values
        if len(readers_field_dict) > len(record):
            writer_fields = [f['name'] for f in writer_schema['fields']]
            for field_name, field in iteritems(readers_field_dict):
                if field_name not in writer_fields:
                    default = field.get('default')
                    if 'default' in field:
                        record[field['name']] = default
                    else:
                        msg = 'No default value for %s' % field['name']
                        raise SchemaResolutionError(msg)

    return record


LOGICAL_READERS = {
    'long-timestamp-millis': read_timestamp_millis,
    'long-timestamp-micros': read_timestamp_micros,
    'int-date': read_date,
    'bytes-decimal': read_bytes_decimal
}

READERS = {
    'null': read_null,
    'boolean': read_boolean,
    'string': read_utf8,
    'int': read_long,
    'long': read_long,
    'float': read_float,
    'double': read_double,
    'bytes': read_bytes,
    'fixed': read_fixed,
    'enum': read_enum,
    'array': read_array,
    'map': read_map,
    'union': read_union,
    'error_union': read_union,
    'record': read_record,
    'error': read_record,
    'request': read_record,
}


def read_data(fo, writer_schema, reader_schema=None):
    """Read data from file object according to schema."""

    record_type = extract_record_type(writer_schema)
    logical_type = extract_logical_type(writer_schema)

    if reader_schema and record_type in AVRO_TYPES:
        match_schemas(writer_schema, reader_schema)
    try:
        data = READERS[record_type](fo, writer_schema, reader_schema)
        if 'logicalType' in writer_schema:
            fn = LOGICAL_READERS[logical_type]
            return fn(data, writer_schema, reader_schema)

        return data
    except StructError:
        raise EOFError('cannot read %s from %s' % (record_type, fo))


def skip_sync(fo, sync_marker):
    """Skip an expected sync marker, complaining if it doesn't match"""
    if fo.read(SYNC_SIZE) != sync_marker:
        raise ValueError('expected sync marker not found')


def null_read_block(fo):
    """Read block in "null" codec."""
    read_long(fo, None)
    return fo


def deflate_read_block(fo):
    """Read block in "deflate" codec."""
    data = read_bytes(fo, None)
    # -15 is the log of the window size; negative indicates "raw" (no
    # zlib headers) decompression.  See zlib.h.
    return MemoryIO(decompress(data, -15))


BLOCK_READERS = {
    'null': null_read_block,
    'deflate': deflate_read_block
}

try:
    import snappy

    def snappy_read_block(fo):
        length = read_long(fo, None)
        data = fo.read(length - 4)
        fo.read(4)  # CRC
        return MemoryIO(snappy.decompress(data))

    BLOCK_READERS['snappy'] = snappy_read_block
except ImportError:
    pass


def _iter_avro(fo, header, codec, writer_schema, reader_schema):
    """Return iterator over avro records."""
    sync_marker = header['sync']
    # Value in schema is bytes

    read_block = BLOCK_READERS.get(codec)
    if not read_block:
        raise ValueError('Unrecognized codec: %r' % codec)

    block_count = 0
    while True:
        block_count = read_long(fo, None)
        block_fo = read_block(fo)

        for i in xrange(block_count):
            yield read_data(block_fo, writer_schema, reader_schema)

        skip_sync(fo, sync_marker)


class iter_avro:
    """Iterator over avro file."""

    def __init__(self, fo, reader_schema=None):
        """Creates a new iterator

        Paramaters
        ----------
        fo: file like
            Input stream
        reader_schema: dict, optional
            Reader schema

        Example
        -------
        >>> with open('some-file.avro', 'rb') as fo:
        >>>     avro = iter_avro(fo)
        >>>     schema = avro.schema
        >>>     for record in avro:
        >>>         process_record(record)
        """
        self.fo = fo
        try:
            self._header = read_data(fo, HEADER_SCHEMA)
        except StopIteration:
            raise ValueError('cannot read header - is it an avro file?')

        # `meta` values are bytes. So, the actual decoding has to be external.
        self.metadata = \
            dict((k, btou(v)) for k, v in iteritems(self._header['meta']))

        self.schema = self.writer_schema = \
            json.loads(self.metadata['avro.schema'])
        self.codec = self.metadata.get('avro.codec', 'null')
        self.reader_schema = reader_schema

        acquaint_schema(self.writer_schema, READERS)
        if reader_schema:
            populate_schema_defs(reader_schema)
        self._records = _iter_avro(fo,
                                   self._header,
                                   self.codec,
                                   self.writer_schema,
                                   reader_schema)

    def __iter__(self):
        return self._records

    def next(self):
        return next(self._records)

    __next__ = next


def schemaless_reader(fo, schema):
    """Reads a single record writen using the schemaless_writer

    Paramaters
    ----------
    fo: file like
        Input stream
    schema: dict
        Reader schema
    """
    acquaint_schema(schema, READERS)
    return read_data(fo, schema)


def is_avro(path_or_buffer):
    """Return True if path (or buffer) points to an Avro file.

    Paramaters
    ----------
    path_or_buffer: path to file or file line object
        Path to file
    """
    if is_str(path_or_buffer):
        fp = open(path_or_buffer, 'rb')
        close = True
    else:
        fp = path_or_buffer
        close = False

    try:
        header = fp.read(len(MAGIC))
        return header == MAGIC
    finally:
        if close:
            fp.close()
