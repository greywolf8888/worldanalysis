"""Recover supported original pictures from the streams of an older Word file."""
from hashlib import sha256
from io import BytesIO
import re
from struct import Struct

from PIL import Image

from archive_media import archive_output, list_seven_zip_members, seven_zip_tool


COMPOUND_HEADER = bytes.fromhex("d0cf11e0a1b11ae1")
WORD_STREAMS = ("Data", "0Table", "1Table", "WordDocument")
RECORD_HEADER = Struct("<HHI")
BLIP_FORMATS = {
    0xF01D: ("JPEG", {0x46A: 17, 0x46B: 33, 0x6E2: 17, 0x6E3: 33}),
    0xF01E: ("PNG", {0x6E0: 17, 0x6E1: 33}),
    0xF029: ("TIFF", {0x6E4: 17, 0x6E5: 33}),
    0xF02A: ("JPEG", {0x46A: 17, 0x46B: 33, 0x6E2: 17, 0x6E3: 33}),
}
OTHER_BLIPS = {
    0xF01A: {0x3D4, 0x3D5},
    0xF01B: {0x216, 0x217},
    0xF01C: {0x542, 0x543},
    0xF01F: {0x7A8, 0x7A9},
}
RECORD_TYPES = re.compile(b"|".join(re.escape(record_type.to_bytes(2, "little"))
                                   for record_type in (*BLIP_FORMATS, *OTHER_BLIPS)))
SIGNATURES = {"JPEG": (b"\xff\xd8\xff",), "PNG": (b"\x89PNG\r\n\x1a\n",),
              "TIFF": (b"II*\x00", b"MM\x00*")}
SOURCE_CACHE_VERSION = "word-pictures-v1"
MAX_STREAM_BYTES = 128 * 1024**2


def scan_word_blips(streams):
    pictures = {}
    gaps = []
    for name, data in streams:
        consumed = 0
        for found in RECORD_TYPES.finditer(data):
            offset = found.start() - 2
            if offset < consumed or offset + RECORD_HEADER.size > len(data):
                continue
            version_and_instance, record_type, length = RECORD_HEADER.unpack_from(data, offset)
            if version_and_instance & 15 or not length:
                continue
            instance = version_and_instance >> 4
            start = offset + RECORD_HEADER.size
            end = start + length
            if record_type in OTHER_BLIPS:
                if instance in OTHER_BLIPS[record_type] and end <= len(data):
                    gaps.append({"reason": f"旧版 Word {name} 原图记录 {offset} 需核原格式 0x{record_type:04X}"})
                    consumed = end
                continue
            image_format, headers = BLIP_FORMATS[record_type]
            if instance not in headers or length <= headers[instance]:
                continue
            payload_start = start + headers[instance]
            if not data.startswith(SIGNATURES[image_format], payload_start):
                continue
            location = {"stream": name, "record_offset": offset, "payload_range": [payload_start, end]}
            if end > len(data):
                gaps.append({"reason": f"旧版 Word {name} 原图记录 {offset} 超出原件数据范围"})
                continue
            consumed = end
            payload = data[payload_start:end]
            try:
                with Image.open(BytesIO(payload)) as image:
                    if image.format != image_format:
                        raise ValueError("图片格式与记录不一致")
                    dimensions = list(image.size)
                    image.verify()
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
                gaps.append({"reason": f"旧版 Word {name} 原图记录 {offset} 无法解码。{error}"})
                continue
            digest = sha256(payload).hexdigest()
            if digest not in pictures:
                pictures[digest] = ({"picture_number": len(pictures) + 1, "format": image_format,
                                     "sha256": digest, "original_wh": dimensions, "occurrences": []}, payload)
            pictures[digest][0]["occurrences"].append(location)
    return list(pictures.values()), gaps


def read_word_pictures(file):
    with file.open("rb") as source:
        if source.read(len(COMPOUND_HEADER)) != COMPOUND_HEADER:
            return [], []
    tool = seven_zip_tool()
    if not tool:
        raise ValueError("旧版 Word 内嵌原图需要本机 7-Zip")
    members = {member.name: member for member in list_seven_zip_members(file, tool)
               if member.name in WORD_STREAMS and member.ordinary}

    def streams():
        for name in WORD_STREAMS:
            member = members.get(name)
            if not member or not member.size:
                continue
            if member.size > MAX_STREAM_BYTES:
                raise ValueError(f"旧版 Word 的 {name} 流超出本地原图提取范围")
            command = [tool, "x", "-so", "-spd", "-bd", "--", str(file), member.seven_zip_name]
            with archive_output(command, 35) as output:
                data = output.read(member.size + 1)
            if len(data) != member.size:
                raise ValueError(f"旧版 Word 的 {name} 流与原索引长度不一致")
            yield name, data

    return scan_word_blips(streams())
