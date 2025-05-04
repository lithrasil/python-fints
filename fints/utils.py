import base64
import json
import re
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime
try:
    from enum import Enum, EnumType
except ImportError:
    from enum import Enum, EnumMeta as EnumType

from .models import StatementOfHoldings
import mt940
from mt940.processors import DETAIL_KEYS
import collections

from .models import Holding


def _parse_mt940_details(detail_str, space=False):
    result = collections.defaultdict(list)

    tmp = collections.OrderedDict()
    segment = ''
    segment_type = ''

    for index, char in enumerate(detail_str):
        if char != '?':
            segment += char
            continue

        if index + 2 >= len(detail_str):
            break

        tmp[segment_type] = segment if not segment_type else segment[2:]
        segment_type = detail_str[index + 1] + detail_str[index + 2]
        segment = ''

    if segment_type:  # pragma: no branch
        tmp[segment_type] = segment if not segment_type else segment[2:]

    for key, value in tmp.items():
        if key in DETAIL_KEYS:
            result[DETAIL_KEYS[key]].append(value)
        elif key == '33':
            key32 = DETAIL_KEYS['32']
            result[key32].append(value)
        elif key.startswith('2'):
            key20 = DETAIL_KEYS['20']
            result[key20].append(value)
        elif key in {'60', '61', '62', '63', '64', '65'}:
            key60 = DETAIL_KEYS['60']
            result[key60].append(value)

    joined_result = dict()
    for key in DETAIL_KEYS.values():
        # Add actual line breaks for the purpose field
        if key == 'purpose':
            separator = '\n'
        else:
            if space:
                separator = ' '
            else:
                separator = ''
        if space:
            value = separator.join(result[key])
        else:
            value = separator.join(result[key])

        joined_result[key] = value or None

    return joined_result

def mt940_to_array(data):       
    # The data string might contain multiple MT940 strings separated by a new line character and "-"
    # Split this string and parse each MT940 individually   
    
    # Override the _parse_mt940_details function in the mt940 module
    # so that line breaks are added to the purpose field
    mt940.processors._parse_mt940_details = _parse_mt940_details
    
    mt940_split = re.split(r'(?<=\r\n-)(?=\r\n)', data)
    result = []
    for mt940_string in mt940_split:   
        if mt940_string == '':
            break     
        mt940_string = mt940_string.replace("@@", "\r\n")
        mt940_string = mt940_string.replace("-0000", "+0000")
        transactions = mt940.models.Transactions()
        transactions.parse(mt940_string)
        result.append(transactions)
        
    return result


def classproperty(f):
    class fx:
        def __init__(self, getter):
            self.getter = getter
        def __get__(self, obj, type=None):
            return self.getter(type)
    return fx(f)


def compress_datablob(magic: bytes, version: int, data: dict):
    data = dict(data)
    for k, v in data.items():
        if k.endswith("_bin"):
            if v:
                data[k] = base64.b64encode(v).decode("us-ascii")
    serialized = json.dumps(data).encode('utf-8')
    compressed = zlib.compress(serialized, 9)
    return b';'.join([magic, b'1', str(version).encode('us-ascii'), compressed])


def decompress_datablob(magic: bytes, blob: bytes, obj: object = None):
    if not blob.startswith(magic):
        raise ValueError("Incorrect data blob")
    s = blob.split(b';', 3)
    if len(s) != 4:
        raise ValueError("Incorrect data blob")
    if not s[1].isdigit() or not s[2].isdigit():
        raise ValueError("Incorrect data blob")
    encoding_version = int(s[1].decode('us-ascii'), 10)
    blob_version = int(s[2].decode('us-ascii'), 10)

    if encoding_version != 1:
        raise ValueError("Unsupported encoding version {}".format(encoding_version))

    decompressed = zlib.decompress(s[3])
    data = json.loads(decompressed.decode('utf-8'))
    for k, v in data.items():
        if k.endswith("_bin"):
            if v:
                data[k] = base64.b64decode(v.encode('us-ascii'))

    if obj:
        setfunc = getattr(obj, "_set_data_v{}".format(blob_version), None)
        if not setfunc:
            raise ValueError("Unknown data blob version")

        setfunc(data)
    else:
        return blob_version, data


class SubclassesMixin:
    @classmethod
    def _all_subclasses(cls):
        for subcls in cls.__subclasses__():
            yield from subcls._all_subclasses()
        yield cls


class DocTypeMixin:
    _DOC_TYPE = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        type_ = self._DOC_TYPE
        if type_ is None:
            if isinstance(getattr(self, 'type', None), type):
                type_ = getattr(self, 'type')

        if type_ is not None:
            if not self.__doc__:
                self.__doc__ = ""

            name = type_.__name__
            if type_.__module__ != 'builtins':
                name = "{}.{}".format(type_.__module__, name)

            self.__doc__ = self.__doc__ + "\n\n:type: :class:`{}`".format(name)


class FieldRenderFormatStringMixin:
    _FORMAT_STRING = None

    def _render_value(self, value):
        retval = self._FORMAT_STRING.format(value)
        self._check_value_length(retval)

        return retval


class FixedLengthMixin:
    _FIXED_LENGTH = [None, None, None]
    _DOC_TYPE = str

    def __init__(self, *args, **kwargs):
        for i, a in enumerate(('length', 'min_length', 'max_length')):
            kwargs[a] = self._FIXED_LENGTH[i] if len(self._FIXED_LENGTH) > i else None

        super().__init__(*args, **kwargs)


class ShortReprMixin:
    def __repr__(self):
        return "{}{}({})".format(
            "{}.".format(self.__class__.__module__),
            self.__class__.__name__,
            ", ".join(
                ("{!r}".format(value) if not name.startswith("_") else "{}={!r}".format(name, value))
                for (name, value) in self._repr_items
            )
        )

    def print_nested(self, stream=None, level=0, indent="    ", prefix="", first_level_indent=True, trailer="", print_doc=True, first_line_suffix=""):
        stream.write(
            ( (prefix + level*indent) if first_level_indent else "")
            + "{!r}{}{}\n".format(self, trailer, first_line_suffix)
        )

class MT535_Miniparser:
    re_identification = re.compile(r"^:35B:ISIN\s(.*)\|(.*)\|(.*)$")
    re_marketprice = re.compile(r"^:90B::MRKT\/\/ACTU\/([A-Z]{3})(\d*),{1}(\d*)$")
    re_pricedate = re.compile(r"^:98A::PRIC\/\/(\d*)$")
    re_pricedate_time = re.compile(r"^:98C::PRIC\/\/(\d*)$")
    re_70c = re.compile(r"^:70C::SUBB//.*? (\d+\.\d+)([A-Z]{3}) (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)")
    re_pieces = re.compile(r"^:93B::AGGR\/\/UNIT\/(\d*),(\d*)$")
    re_totalvalue = re.compile(r"^:19A::HOLD\/\/([A-Z]{3})(\d*),{1}(\d*)$")
    re_depot_totalvalue = re.compile(r"^:19A::HOLP\/\/([A-Z]{3})(\d*),{1}(\d*)$")    
    re_acquisitionprice = re.compile(r"^:70E::HOLD\/\/\d*STK(?:\+\+\+\+\d{8}\+)?\|2(\d*?),{1}(\d*?)\+([A-Z]{3})$")

    def parse(self, lines):
        retval = StatementOfHoldings()
        # First: Collapse multiline clauses into one clause
        clauses = self.collapse_multilines(lines)
        # Second: Scan sequence of clauses for financial instrument
        # sections
        finsegs = self.grab_financial_instrument_segments(clauses)
        # Third: Extract financial instrument data
        for finseg in finsegs:
            isin, name, market_price, price_symbol, price_date, pieces, acquisitionprice = (None,)*7
            for clause in finseg:
                # identification of instrument
                # e.g. ':35B:ISIN LU0635178014|/DE/ETF127|COMS.-MSCI EM.M.T.U.ETF I'
                m = self.re_identification.match(clause)
                if m:
                    isin = m.group(1)
                    wkn = m.group(2)
                    if wkn.startswith('/DE/'):
                        wkn = wkn[4:]
                    else:
                        wkn = None
                    name = m.group(3)
                # current market price
                # e.g. ':90B::MRKT//ACTU/EUR38,82'
                m = self.re_marketprice.match(clause)
                if m:
                    price_symbol = m.group(1)
                    market_price = float(m.group(2) + "." + m.group(3))
                # date of market price
                # e.g. ':98A::PRIC//20170428'
                m = self.re_pricedate.match(clause)
                if m:
                    price_date = datetime.strptime(m.group(1), "%Y%m%d")
                
                m = self.re_pricedate_time.match(clause)
                if m:
                    price_date = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
                    
                # current market price, date and time from segment 70C
                # e.g. ' :70C:: SUBB//1 NVIDIA CORP. DL-,0011213 EDE 122.400000000EUR 2025-02-27T19:13:26.8914 119.94EUR US67066G1040, 1/SHS'
                m = self.re_70c.match(clause)
                if m:
                    market_price = float(m.group(1))    
                    price_symbol = m.group(2)
                    price_date = datetime.strptime(m.group(3), "%Y-%m-%dT%H:%M:%S.%f")                        
                
                # number of pieces
                # e.g. ':93B::AGGR//UNIT/16,8211'
                m = self.re_pieces.match(clause)
                if m:
                    pieces = float(m.group(1) + "." + m.group(2))
                # total value of holding
                # e.g. ':19A::HOLD//EUR970,17'
                m = self.re_totalvalue.match(clause)
                if m:
                    total_value = float(m.group(2) + "." + m.group(3))
                # Acquisition price
                # e.g ':70E::HOLD//1STK23,968293+EUR'
                m = self.re_acquisitionprice.match(clause)
                if m:
                    acquisitionprice = float(m.group(1) + '.' + m.group(2))

            # processed all clauses
            retval.holdings.append(
                Holding(
                    ISIN=isin, WKN=wkn, name=name, market_value=market_price,
                    value_symbol=price_symbol, valuation_date=price_date,
                    pieces=pieces, total_value=total_value,
                    acquisitionprice=acquisitionprice))
            
        add_info_segments = self.grab_additional_info_segments(clauses)
        
        for add_info_seg in add_info_segments:
            m = self.re_depot_totalvalue.match(add_info_seg)
            if m:
                retval.total_value = float(m.group(2) + "." + m.group(3))
            
        return retval

    def collapse_multilines(self, lines):
        clauses = []
        prevline = ""
        for line in lines:
            if line.startswith(":"):
                if prevline != "":
                    clauses.append(prevline)
                prevline = line
            elif line.startswith("-"):
                # last line
                clauses.append(prevline)
                clauses.append(line)
            else:
                prevline += "|{}".format(line)
        return clauses

    def grab_financial_instrument_segments(self, clauses):
        retval = []
        stack = []
        within_financial_instrument = False
        for clause in clauses:
            if clause.startswith(":16R:FIN"):
                # start of financial instrument
                within_financial_instrument = True
            elif clause.startswith(":16S:FIN"):
                # end of financial instrument - move stack over to
                # return value
                retval.append(stack)
                stack = []
                within_financial_instrument = False
            else:
                if within_financial_instrument:
                    stack.append(clause)
        return retval
    
    def grab_additional_info_segments(self, clauses):
        retval = []
        
        within_additional_info = False
        for clause in clauses:
            if clause.startswith(":16R:ADDINFO"):                
                within_additional_info = True
            elif clause.startswith(":16S:ADDINFO"):
                return retval
            else:
                if within_additional_info:
                    retval.append(clause)
        return retval


class Password(str):
    protected = False

    def __init__(self, value):
        self.value = value
        self.blocked = False

    @classmethod
    @contextmanager
    def protect(cls):
        try:
            cls.protected = True
            yield None
        finally:
            cls.protected = False

    def block(self):
        self.blocked = True

    def __str__(self):
        if self.blocked and not self.protected:
            raise Exception("Refusing to use PIN after block")
        return '***' if self.protected else str(self.value)

    def __repr__(self):
        return self.__str__().__repr__()

    def __add__(self, other):
        return self.__str__().__add__(other)

    def replace(self, *args, **kwargs):
        return self.__str__().replace(*args, **kwargs)


class RepresentableEnum(Enum):
    def __repr__(self):
        return "{}.{}.{}".format(self.__class__.__module__, self.__class__.__name__, self.name)

    def __str__(self):
        return self.value


def minimal_interactive_cli_bootstrap(client):
    """
    This is something you usually implement yourself to ask your user in a nice, user-friendly way about these things.
    This is mainly included to keep examples in the documentation simple and allow you to get started quickly.
    """
    # Fetch available TAN mechanisms by the bank, if we don't know it already. If the client was created with cached data,
    # the function is already set.
    if not client.get_current_tan_mechanism():
        client.fetch_tan_mechanisms()
        mechanisms = list(client.get_tan_mechanisms().items())
        if len(mechanisms) > 1:
            print("Multiple tan mechanisms available. Which one do you prefer?")
            for i, m in enumerate(mechanisms):
                print(i, "Function {p.security_function}: {p.name}".format(p=m[1]))
            choice = input("Choice: ").strip()
            client.set_tan_mechanism(mechanisms[int(choice)][0])

    if client.selected_tan_medium is None and client.is_tan_media_required():
        print("We need the name of the TAN medium, let's fetch them from the bank")
        m = client.get_tan_media()
        if len(m[1]) == 1:
            client.set_tan_medium(m[1][0])
        elif len(m[1]) == 0:
            # This is a workaround for when the dialog already contains return code 3955.
            # This occurs with e.g. Sparkasse Heidelberg, which apparently does not require us to choose a
            # medium for pushTAN but is totally fine with keeping "" as a TAN medium.
            client.selected_tan_medium = ""
        else:
            print("Multiple tan media available. Which one do you prefer?")
            for i, mm in enumerate(m[1]):
                print(i,
                      "Medium {p.tan_medium_name}: Phone no. {p.mobile_number_masked}, Last used {p.last_use}".format(
                          p=mm))
            choice = input("Choice: ").strip()
            client.set_tan_medium(m[1][int(choice)])


class LogConfiguration(threading.local):
    """Thread-local configuration object to guide log output.

    reduced: Reduce verbosity of logging output by suppressing the encrypting/signature elements and outputting the payload only.
    """
    def __init__(self, reduced=False):
        super().__init__()
        self.reduced = reduced

    @staticmethod
    def set(reduced=False):
        """Permanently change the log configuration for this thread."""
        log_configuration.reduced = reduced

    @staticmethod
    @contextmanager
    def changed(reduced=False):
        """Temporarily change the log configuration for this thread."""
        old_reduced = log_configuration.reduced
        log_configuration.set(reduced=reduced)
        yield
        log_configuration.set(reduced=old_reduced)


log_configuration = LogConfiguration()

try:
    from enum_tools import document_enum

    doc_enum = document_enum
except ImportError:
    def doc_enum(an_enum: EnumType) -> EnumType:
        return an_enum
