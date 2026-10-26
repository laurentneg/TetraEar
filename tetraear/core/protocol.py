"""
TETRA Protocol Layer Parser

This module implements PHY, MAC, and higher layer parsing for TETRA frames.
Parses bursts, slots, frames, and superframes according to ETSI TETRA standards.

Classes:
    TetraProtocolParser: Main protocol parser for TETRA frames
    TetraBurst: Represents a TETRA burst
    MacPDU: Represents a MAC layer PDU
    CallMetadata: Metadata for TETRA calls

Enums:
    BurstType: TETRA burst types
    ChannelType: TETRA logical channel types
    PDUType: MAC PDU types

Example:
    >>> from tetraear.core.protocol import TetraProtocolParser
    >>> parser = TetraProtocolParser()
    >>> burst = parser.parse_burst(symbols, slot_number=0)
"""

import numpy as np
from bitstring import BitArray
import logging
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from enum import Enum
from collections import Counter

logger = logging.getLogger(__name__)


MACPDU_LEN_2ND_STOLEN = -2
MACPDU_LEN_START_FRAG = -1


class BurstType(Enum):
    """TETRA burst types."""
    NormalUplink = 1
    NormalDownlink = 2
    ControlUplink = 3
    ControlDownlink = 4
    Synchronization = 5
    Linearization = 6


class ChannelType(Enum):
    """TETRA logical channel types."""
    TCH = "Traffic Channel"
    STCH = "Stealing Channel"
    SCH = "Signaling Channel"
    AACH = "Associated Control Channel"
    BSCH = "Broadcast Synchronization Channel"
    BNCH = "Broadcast Network Channel"
    
    
class PDUType(Enum):
    """MAC PDU types."""
    MAC_RESOURCE = 0
    MAC_FRAG = 1
    MAC_END = 2
    MAC_BROADCAST = 3
    MAC_SUPPL = 4
    MAC_U_SIGNAL = 5
    MAC_DATA = 6
    MAC_U_BLK = 7


@dataclass
class TetraBurst:
    """Represents a TETRA burst (255 symbols)."""
    burst_type: BurstType
    slot_number: int
    frame_number: int
    training_sequence: np.ndarray
    data_bits: np.ndarray
    crc_ok: bool
    scrambling_code: int = 0
    colour_code: int = 0
    

@dataclass
class TetraSlot:
    """Represents a TETRA time slot (14.167ms, 255 symbols)."""
    slot_number: int  # 0-3 within frame
    frame_number: int
    burst: TetraBurst
    channel_type: ChannelType
    encrypted: bool = False
    encryption_mode: int = 0


@dataclass
class TetraFrame:
    """Represents a TETRA frame (4 slots = 56.67ms)."""
    frame_number: int  # 0-17 within multiframe
    slots: List[TetraSlot]
    multiframe_number: int = 0
    
    
@dataclass
class TetraMultiframe:
    """Represents a TETRA multiframe (18 frames = 1.02 seconds)."""
    multiframe_number: int
    frames: List[TetraFrame]


@dataclass
class TetraHyperframe:
    """Represents a TETRA hyperframe (60 multiframes = 61.2 seconds)."""
    hyperframe_number: int
    multiframes: List[TetraMultiframe]


@dataclass
class MacPDU:
    """MAC layer PDU."""
    pdu_type: PDUType
    encrypted: bool
    address: Optional[int]
    length: int
    data: bytes
    data_bits: Optional[np.ndarray] = None
    tm_sdu_bits: Optional[np.ndarray] = None
    fill_bits: int = 0
    encryption_mode: int = 0  # 0=Clear, 1=Class2, 2=Class3, 3=Reserved
    reassembled_data: Optional[bytes] = None  # For fragmented messages
    crc_ok: Optional[bool] = None
    extra: Optional[dict] = None
    

@dataclass
class CallMetadata:
    """Call setup/teardown metadata."""
    call_type: str  # "Voice", "Data", "Group", "Individual"
    talkgroup_id: Optional[int]
    source_ssi: Optional[int]  # Subscriber Station Identity
    dest_ssi: Optional[int]
    channel_allocated: Optional[int]
    call_identifier: Optional[int] = None
    call_priority: int = 0
    mcc: Optional[int] = None
    mnc: Optional[int] = None
    duplex_mode: str = "simplex"
    encryption_enabled: bool = False
    encryption_algorithm: Optional[str] = None


class TetraProtocolParser:
    """
    TETRA protocol parser implementing PHY + MAC + higher layers.
    Demonstrates OpenEar-style decoding capabilities.
    """
    
    # TETRA timing constants
    SYMBOLS_PER_SLOT = 255
    SLOTS_PER_FRAME = 4
    FRAMES_PER_MULTIFRAME = 18
    MULTIFRAMES_PER_HYPERFRAME = 60
    
    # Training sequences for burst synchronization
    TRAINING_SEQUENCES = {
        1: [0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0, 1, 1],
        2: [0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0, 1],
        3: [0, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0],
    }
    
    # Sync patterns
    SYNC_CONTINUOUS_DOWNLINK = [1, 1, 0, 1, 0, 0, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 1, 0, 0]
    SYNC_DISCONTINUOUS_DOWNLINK = [0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 1, 0, 0, 1, 1]
    
    def __init__(self):
        """Initialize protocol parser."""
        self.current_frame_number = 0
        self.current_multiframe = 0
        self.current_hyperframe = 0
        self.mcc = None  # Mobile Country Code
        self.mnc = None  # Mobile Network Code
        self.la = None   # Location Area
        self.colour_code = None
        
        # Statistics
        self.stats = {
            'total_bursts': 0,
            'crc_pass': 0,
            'crc_fail': 0,
            'clear_mode_frames': 0,
            'encrypted_frames': 0,
            'decrypted_frames': 0,
            'voice_calls': 0,
            'data_messages': 0,
            'control_messages': 0,
        }
        
        # Fragmentation handling
        self.fragment_buffer = bytearray()
        self.fragment_metadata = {}
        self._network_votes: Counter[tuple[int, int, int]] = Counter()
        self._llc_defrag: Dict[int, Dict[str, object]] = {}
        self._mac_frag_active = False
        self._mac_frag_bits: Optional[np.ndarray] = None
        self._mac_frag_extra: Dict[str, object] = {}
        self._ssi_votes: Counter[int] = Counter()

    def _record_network_candidate(self, mcc: int, mnc: int, colour_code: int, *, strong: bool) -> None:
        """Record MCC/MNC candidates and only lock in after repeats unless strong."""
        if strong:
            self.mcc = mcc
            self.mnc = mnc
            self.colour_code = colour_code
            return

        key = (mcc, mnc, colour_code)
        self._network_votes[key] += 1
        if self._network_votes[key] >= 3:
            self.mcc = mcc
            self.mnc = mnc
            self.colour_code = colour_code
        
    def parse_burst(self, symbols: np.ndarray, slot_number: int = 0) -> Optional[TetraBurst]:
        """
        Parse a TETRA burst (255 symbols).
        
        Args:
            symbols: Symbol stream (255 symbols expected)
            slot_number: Slot number (0-3)
            
        Returns:
            Parsed TetraBurst or None if invalid
        """
        if len(symbols) < self.SYMBOLS_PER_SLOT:
            logger.warning(f"Insufficient symbols for burst: {len(symbols)} < {self.SYMBOLS_PER_SLOT}")
            return None
        
        # Extract burst
        burst_symbols = symbols[:self.SYMBOLS_PER_SLOT]
        
        # Convert symbols to bits (2 bits per π/4-DQPSK symbol)
        bits = []
        for sym in burst_symbols:
            bits.extend([int(sym >> 1 & 1), int(sym & 1)])
        bits = np.array(bits)
        
        # Detect burst type from training sequence position
        burst_type = self._detect_burst_type(bits)
        
        # Extract training sequence
        training_seq = self._extract_training_sequence(bits, burst_type)
        
        # Extract data bits (excluding training sequence and tail bits)
        data_bits = self._extract_data_bits(bits, burst_type)
        
        # Check CRC
        crc_ok = self._check_crc(data_bits)
        
        self.stats['total_bursts'] += 1
        if crc_ok:
            self.stats['crc_pass'] += 1
        else:
            self.stats['crc_fail'] += 1
        
        burst = TetraBurst(
            burst_type=burst_type,
            slot_number=slot_number,
            frame_number=self.current_frame_number,
            training_sequence=training_seq,
            data_bits=data_bits,
            crc_ok=crc_ok,
            colour_code=self.colour_code or 0
        )
        
        return burst
    
    def _detect_burst_type(self, bits: np.ndarray) -> BurstType:
        """Detect burst type from training sequence position."""
        # Check for sync burst (training sequence at specific position)
        sync_pos = len(bits) // 2
        if self._check_sync_pattern(bits[sync_pos:sync_pos+22]):
            return BurstType.Synchronization
        
        # Default to normal downlink
        return BurstType.NormalDownlink
    
    def _check_sync_pattern(self, bits: np.ndarray) -> bool:
        """Check if bits match sync pattern."""
        if len(bits) < 22:
            return False
        
        # Check both sync patterns
        match_cont = np.sum(bits[:22] == self.SYNC_CONTINUOUS_DOWNLINK) / 22
        match_disc = np.sum(bits[:22] == self.SYNC_DISCONTINUOUS_DOWNLINK) / 22
        
        return max(match_cont, match_disc) > 0.8
    
    def _extract_training_sequence(self, bits: np.ndarray, burst_type: BurstType) -> np.ndarray:
        """Extract training sequence from burst."""
        # Training sequence is typically in the middle of the burst.
        # Use 11 symbols (22 bits) for normal/sync bursts based on TS length.
        training_len_bits = 22
        if burst_type == BurstType.Synchronization:
            # Sync burst: training at position ~108 symbols -> 216 bits
            start = 216
        else:
            # Normal burst: training at position ~108 symbols -> 216 bits
            start = 216
        end = min(len(bits), start + training_len_bits)
        return bits[start:end]
    
    def _extract_data_bits(self, bits: np.ndarray, burst_type: BurstType) -> np.ndarray:
        """Extract data bits from burst (excluding training and tail)."""
        # Normal burst: 432 bits (2 x 108 symbols) excluding training sequence
        if burst_type == BurstType.NormalDownlink or burst_type == BurstType.NormalUplink:
            # First block: 108 symbols = 216 bits
            # Training: 11 symbols = 22 bits
            # Second block: 108 symbols = 216 bits
            first_end = 108 * 2
            training_end = first_end + 11 * 2
            second_end = training_end + 108 * 2
            first_block = bits[0:first_end]
            second_block = bits[training_end:second_end]
            return np.concatenate([first_block, second_block])
        
        # For other burst types, return all bits
        return bits
    
    def _check_crc(self, bits: np.ndarray) -> bool:
        """
        Check CRC-16-CCITT for data integrity.
        Simplified check - TETRA CRC is complex, so we use heuristics.
        """
        if len(bits) < 16:
            return False
        
        # SIMPLIFIED: Use a soft CRC check since we do not do full channel decoding.
        ones = int(np.sum(bits))
        zeros = len(bits) - ones
        if ones == 0 or zeros == 0:
            return False

        try:
            payload = bits[:-16]
            received_crc = bits[-16:]
            calculated_crc = self._calculate_crc16(payload)
            
            errors = int(np.sum(calculated_crc != received_crc))
            if errors == 0:
                return True

            # Allow a small error budget without channel decoding.
            if errors <= 2:
                return True

            # Try reversed bit order to handle endianness mismatches.
            reversed_crc = self._calculate_crc16(payload[::-1])
            errors_rev = int(np.sum(reversed_crc != received_crc))
            if errors_rev == 0:
                return True
            if errors_rev <= 2:
                return True
        except Exception:
            return False
        
        return False
    
    def _calculate_crc16(self, bits: np.ndarray) -> np.ndarray:
        """Calculate CRC-16-CCITT (polynomial 0x1021)."""
        polynomial = 0x1021
        crc = 0xFFFF
        
        for bit in bits:
            crc ^= (int(bit) << 15)
            for _ in range(1):
                if crc & 0x8000:
                    crc = (crc << 1) ^ polynomial
                else:
                    crc <<= 1
                crc &= 0xFFFF
        
        # Convert to bits
        crc_bits = [(crc >> i) & 1 for i in range(15, -1, -1)]
        return np.array(crc_bits)
    
    def parse_mac_pdu(self, bits: np.ndarray, *, crc_ok: Optional[bool] = None) -> Optional[MacPDU]:
        """
        Parse MAC layer PDU.
        Handles fragmentation (MAC-RESOURCE, MAC-FRAG, MAC-END).
        
        Args:
            bits: Data bits from burst
            crc_ok: Optional CRC validity for the burst carrying this PDU
            
        Returns:
            Parsed MacPDU or None
        """
        if bits is None or len(bits) < 8:
            return None

        if not isinstance(bits, np.ndarray):
            bits = np.array(bits, dtype=np.int16)
        else:
            bits = bits.astype(np.int16, copy=False)

        if np.any((bits != 0) & (bits != 1)):
            bits = (bits < 0).astype(np.uint8)
        else:
            bits = bits.astype(np.uint8, copy=False)

        def _bits_to_uint(bit_arr: np.ndarray, start: int, length: int) -> int:
            val = 0
            end = min(start + length, len(bit_arr))
            for bit in bit_arr[start:end]:
                val = (val << 1) | (int(bit) & 1)
            return val

        def _bits_to_bytes(bit_arr: np.ndarray) -> bytes:
            if bit_arr is None or len(bit_arr) == 0:
                return b""
            out = bytearray()
            for i in range(0, len(bit_arr), 8):
                chunk = bit_arr[i : i + 8]
                if len(chunk) < 8:
                    chunk = np.pad(chunk, (0, 8 - len(chunk)))
                val = 0
                for bit in chunk:
                    val = (val << 1) | (int(bit) & 1)
                out.append(val)
            return bytes(out)

        def _decode_length(length_ind: int) -> Optional[int]:
            if length_ind in (0x00, 0x3B, 0x3C):
                return None
            if length_ind <= 0x12:
                return length_ind
            if length_ind <= 0x3A:
                return 18 + (length_ind - 18)
            if length_ind == 0x3E:
                return MACPDU_LEN_2ND_STOLEN
            if length_ind == 0x3F:
                return MACPDU_LEN_START_FRAG
            return None

        def _get_num_fill_bits(bit_arr: np.ndarray, length: int) -> int:
            for i in range(1, length + 1):
                if bit_arr[length - i] == 1:
                    return i
            return 0

        def _parse_chan_alloc(bit_arr: np.ndarray, start: int) -> tuple[dict, int]:
            cur = start
            info: dict = {}
            if cur + 2 > len(bit_arr):
                return info, 0
            info["alloc_type"] = _bits_to_uint(bit_arr, cur, 2); cur += 2
            if cur + 4 > len(bit_arr):
                return info, cur - start
            info["timeslot"] = _bits_to_uint(bit_arr, cur, 4); cur += 4
            if cur + 2 > len(bit_arr):
                return info, cur - start
            info["ul_dl"] = _bits_to_uint(bit_arr, cur, 2); cur += 2
            if cur + 1 > len(bit_arr):
                return info, cur - start
            info["clch_perm"] = int(bit_arr[cur]); cur += 1
            if cur + 1 > len(bit_arr):
                return info, cur - start
            info["cell_chg"] = int(bit_arr[cur]); cur += 1
            if cur + 12 > len(bit_arr):
                return info, cur - start
            info["carrier"] = _bits_to_uint(bit_arr, cur, 12); cur += 12

            if cur >= len(bit_arr):
                return info, cur - start
            ext_carr_pres = int(bit_arr[cur]); cur += 1
            info["ext_carr_pres"] = ext_carr_pres
            if ext_carr_pres:
                if cur + 4 > len(bit_arr):
                    return info, cur - start
                info["freq_band"] = _bits_to_uint(bit_arr, cur, 4); cur += 4
                if cur + 2 > len(bit_arr):
                    return info, cur - start
                info["freq_offset"] = _bits_to_uint(bit_arr, cur, 2); cur += 2
                if cur + 3 > len(bit_arr):
                    return info, cur - start
                info["duplex_spacing"] = _bits_to_uint(bit_arr, cur, 3); cur += 3
                if cur + 1 > len(bit_arr):
                    return info, cur - start
                info["reverse_operation"] = int(bit_arr[cur]); cur += 1

            if cur + 2 > len(bit_arr):
                return info, cur - start
            monit_pattern = _bits_to_uint(bit_arr, cur, 2); cur += 2
            info["monit_pattern"] = monit_pattern
            if monit_pattern == 0 and cur + 2 <= len(bit_arr):
                info["monit_patt_f18"] = _bits_to_uint(bit_arr, cur, 2)
                cur += 2

            if info.get("ul_dl") == 0:
                if cur + 2 > len(bit_arr):
                    return info, cur - start
                info["ul_dl_ass"] = _bits_to_uint(bit_arr, cur, 2); cur += 2
                if cur + 3 > len(bit_arr):
                    return info, cur - start
                info["bandwidth"] = _bits_to_uint(bit_arr, cur, 3); cur += 3
                if cur + 3 > len(bit_arr):
                    return info, cur - start
                info["modulation"] = _bits_to_uint(bit_arr, cur, 3); cur += 3
                if cur + 3 > len(bit_arr):
                    return info, cur - start
                info["max_ul_qam"] = _bits_to_uint(bit_arr, cur, 3); cur += 3
                cur += 3  # reserved
                if cur + 3 > len(bit_arr):
                    return info, cur - start
                info["conf_chan_stat"] = _bits_to_uint(bit_arr, cur, 3); cur += 3
                if cur + 4 > len(bit_arr):
                    return info, cur - start
                info["bs_imbalance"] = _bits_to_uint(bit_arr, cur, 4); cur += 4
                if cur + 5 > len(bit_arr):
                    return info, cur - start
                info["bs_tx_rel"] = _bits_to_uint(bit_arr, cur, 5); cur += 5
                if cur + 2 > len(bit_arr):
                    return info, cur - start
                napping = _bits_to_uint(bit_arr, cur, 2); cur += 2
                info["napping_sts"] = napping
                if napping == 1:
                    cur += 11
                cur += 4
                if cur < len(bit_arr):
                    if bit_arr[cur]:
                        cur += 1 + 16
                    else:
                        cur += 1
                if cur < len(bit_arr):
                    if bit_arr[cur]:
                        cur += 1 + 16
                    else:
                        cur += 1
                cur += 1
            return info, cur - start

        pdu_type_int = _bits_to_uint(bits, 0, 2)
        if pdu_type_int == 0:
            pdu_type = PDUType.MAC_RESOURCE
        elif pdu_type_int == 1:
            pdu_type = PDUType.MAC_FRAG
        elif pdu_type_int == 2:
            pdu_type = PDUType.MAC_BROADCAST
        else:
            pdu_type = PDUType.MAC_SUPPL

        encrypted = False
        address = None
        length = 0
        data_bytes = b""
        data_bits = None
        tm_sdu_bits = None
        extra: dict = {}

        addr_len_by_type = {
            1: 24,
            2: 10,
            3: 24,
            4: 24,
            5: 34,
            6: 30,
            7: 34,
        }

        if pdu_type == PDUType.MAC_RESOURCE:
            cur = 2
            fill_bits = _bits_to_uint(bits, cur, 1); cur += 1
            grant_position = _bits_to_uint(bits, cur, 1); cur += 1
            encryption_mode = _bits_to_uint(bits, cur, 2); cur += 2
            encrypted = encryption_mode > 0
            rand_acc_flag = int(bits[cur]) if cur < len(bits) else 0; cur += 1
            length_ind = _bits_to_uint(bits, cur, 6); cur += 6
            addr_type = _bits_to_uint(bits, cur, 3); cur += 3

            addr_len = addr_len_by_type.get(addr_type, 0)
            usage_marker = None
            if addr_len >= 24 and cur + 24 <= len(bits):
                address = _bits_to_uint(bits, cur, 24)
                if addr_type == 6 and cur + 30 <= len(bits):
                    usage_marker = _bits_to_uint(bits, cur + 24, 6)
            elif addr_len == 10 and cur + 10 <= len(bits):
                address = _bits_to_uint(bits, cur, 10)
            cur += addr_len

            if cur < len(bits):
                power_control_pres = int(bits[cur]); cur += 1
                if power_control_pres:
                    cur += 4
            if cur < len(bits):
                slot_granting_pres = int(bits[cur]); cur += 1
                if slot_granting_pres:
                    cur += 8
            if cur < len(bits):
                chan_alloc_pres = int(bits[cur]); cur += 1
                if chan_alloc_pres:
                    alloc_info, consumed = _parse_chan_alloc(bits, cur)
                    if consumed:
                        cur += consumed
                        extra.update(alloc_info)

            extra.update({
                "fill_bits": fill_bits,
                "grant_position": grant_position,
                "rand_acc_flag": rand_acc_flag,
                "encryption_mode": encryption_mode,
                "length_ind": length_ind,
                "addr_type": addr_type,
                "usage_marker": usage_marker,
            })
            extra["address"] = address
            if address is not None:
                extra["ssi"] = address

            mac_len = _decode_length(length_ind)
            tm_sdu_len = None
            if mac_len is not None and mac_len >= 0:
                tm_sdu_len = mac_len * 8
            elif mac_len == MACPDU_LEN_2ND_STOLEN:
                extra["blk2_stolen"] = True
            elif mac_len == MACPDU_LEN_START_FRAG:
                extra["start_frag"] = True

            if tm_sdu_len is not None:
                end = min(len(bits), cur + tm_sdu_len)
            else:
                end = len(bits)
                if fill_bits and end > 0:
                    end = max(cur, end - _get_num_fill_bits(bits[:end], end))

            tm_sdu_bits = bits[cur:end] if end > cur else None
            if mac_len == MACPDU_LEN_START_FRAG:
                self._mac_frag_active = True
                if tm_sdu_bits is not None and len(tm_sdu_bits) > 0:
                    self._mac_frag_bits = np.array(tm_sdu_bits, copy=True)
                else:
                    self._mac_frag_bits = np.array([], dtype=np.uint8)
                self._mac_frag_extra = {"address": address, **extra}
                tm_sdu_bits = None
            data_bits = tm_sdu_bits
            data_bytes = _bits_to_bytes(data_bits)
            length = len(data_bytes)
            self.fragment_buffer = bytearray(data_bytes)
            self.fragment_metadata = {"address": address, "encrypted": encrypted}
            extra["tm_sdu_bits"] = tm_sdu_bits

        elif pdu_type == PDUType.MAC_BROADCAST:
            broadcast_type = _bits_to_uint(bits, 2, 2)
            extra["broadcast_type"] = broadcast_type
            data_bits = bits[4:]
            data_bytes = _bits_to_bytes(data_bits)
            length = len(data_bytes)
            # SYSINFO (broadcast_type=0): extract Location Area from D-MLE-SYSINFO
            # Layout after MAC header (common osmo-tetra / telive compatible):
            # main_carrier(12) freq_band(4) offset(2) duplex(3) reverse(1)
            # unused(2) n_frames(3) frame18_ext(1) -> LA(14)
            if broadcast_type == 0 and len(bits) >= 4 + 12 + 4 + 2 + 3 + 1 + 2 + 3 + 1 + 14:
                cur = 4
                extra["main_carrier"] = _bits_to_uint(bits, cur, 12); cur += 12
                extra["frequency_band"] = _bits_to_uint(bits, cur, 4); cur += 4
                extra["offset"] = _bits_to_uint(bits, cur, 2); cur += 2
                extra["duplex_spacing"] = _bits_to_uint(bits, cur, 3); cur += 3
                extra["reverse_operation"] = _bits_to_uint(bits, cur, 1); cur += 1
                cur += 2  # unused
                extra["n_frames_continuous"] = _bits_to_uint(bits, cur, 3); cur += 3
                extra["frame18_extension"] = _bits_to_uint(bits, cur, 1); cur += 1
                location_area = _bits_to_uint(bits, cur, 14)
                extra["location_area"] = location_area
                self.la = location_area
                logger.info(
                    "SYSINFO: LA=%s carrier=%s band=%s",
                    location_area,
                    extra.get("main_carrier"),
                    extra.get("frequency_band"),
                )

        elif pdu_type == PDUType.MAC_FRAG:
            frag_end_flag = _bits_to_uint(bits, 2, 1)
            fillbits_present = _bits_to_uint(bits, 3, 1)
            cur = 4

            if frag_end_flag == 0:
                # MAC-FRAG continuation
                end = len(bits)
                if fillbits_present and end > 0:
                    end = max(cur, end - _get_num_fill_bits(bits[:end], end))
                frag_bits = bits[cur:end] if end > cur else None
                if frag_bits is not None and len(frag_bits) > 0:
                    if not self._mac_frag_active:
                        self._mac_frag_active = True
                        self._mac_frag_bits = np.array(frag_bits, copy=True)
                    elif self._mac_frag_bits is None or np.size(self._mac_frag_bits) == 0:
                        self._mac_frag_bits = np.array(frag_bits, copy=True)
                    else:
                        self._mac_frag_bits = np.concatenate([self._mac_frag_bits, frag_bits])
                extra.update({"frag_end": False, "fill_bits": fillbits_present})
                tm_sdu_bits = None
                data_bits = frag_bits
                data_bytes = _bits_to_bytes(frag_bits) if frag_bits is not None else b""
                length = len(data_bytes)
            else:
                # MAC-END, includes length indicator and optional chan alloc
                position_of_grant = _bits_to_uint(bits, cur, 1); cur += 1
                length_ind = _bits_to_uint(bits, cur, 6); cur += 6
                slot_granting = _bits_to_uint(bits, cur, 1); cur += 1
                if slot_granting and cur + 8 <= len(bits):
                    cur += 8
                chan_alloc_pres = _bits_to_uint(bits, cur, 1) if cur < len(bits) else 0
                cur += 1 if cur < len(bits) else 0
                if chan_alloc_pres:
                    alloc_info, consumed = _parse_chan_alloc(bits, cur)
                    if consumed:
                        cur += consumed
                        extra.update(alloc_info)

                tm_sdu_len = length_ind * 8 if length_ind else None
                end = len(bits)
                if tm_sdu_len is not None and tm_sdu_len > 0:
                    end = min(end, cur + tm_sdu_len)
                if fillbits_present and end > 0:
                    end = max(cur, end - _get_num_fill_bits(bits[:end], end))
                frag_bits = bits[cur:end] if end > cur else None

                if self._mac_frag_active and self._mac_frag_bits is not None and frag_bits is not None:
                    tm_sdu_bits = np.concatenate([self._mac_frag_bits, frag_bits])
                    self._mac_frag_active = False
                    self._mac_frag_bits = None
                    if self._mac_frag_extra:
                        if address is None:
                            address = self._mac_frag_extra.get("address")
                        extra = {**self._mac_frag_extra, **extra}
                        self._mac_frag_extra = {}
                else:
                    tm_sdu_bits = frag_bits

                data_bits = tm_sdu_bits
                data_bytes = _bits_to_bytes(data_bits) if data_bits is not None else b""
                length = len(data_bytes)
                extra.update({
                    "frag_end": True,
                    "fill_bits": fillbits_present,
                    "position_of_grant": position_of_grant,
                    "length_ind": length_ind,
                    "slot_granting": slot_granting,
                })

        else:
            data_bits = bits[2:]
            data_bytes = _bits_to_bytes(data_bits)
            length = len(data_bytes)

        if encrypted:
            self.stats['encrypted_frames'] += 1
        else:
            self.stats['clear_mode_frames'] += 1

        pdu = MacPDU(
            pdu_type=pdu_type,
            encrypted=encrypted,
            address=address,
            length=length,
            data=data_bytes,
            data_bits=data_bits,
            tm_sdu_bits=tm_sdu_bits,
            crc_ok=crc_ok,
            extra=extra or None,
        )

        pdu.reassembled_data = bytes(data_bytes) if data_bytes else None
        return pdu

    @staticmethod
    def _is_valid_ssi(value: Optional[int]) -> bool:
        """Validate a 24-bit SSI/GSSI value."""
        if value is None:
            return False
        return 0 < value < 0xFFFFFF and value != 0xFFFFFF

    def _resource_talkgroup(self, resource: dict) -> Optional[int]:
        """Extract SSI/GSSI from resource address if applicable."""
        if not resource:
            return None
        addr_type = resource.get("addr_type")
        if addr_type in (0, 2):
            return None
        talkgroup = resource.get("ssi") or resource.get("address")
        if not self._is_valid_ssi(talkgroup):
            return None
        return talkgroup

    @staticmethod
    def _bits_to_uint(bit_arr: np.ndarray, start: int, length: int, *, lsb: bool = False) -> int:
        """Convert a slice of bits into an integer (MSB-first by default)."""
        val = 0
        end = min(start + length, len(bit_arr))
        if lsb:
            shift = 0
            for bit in bit_arr[start:end]:
                val |= (int(bit) & 1) << shift
                shift += 1
        else:
            for bit in bit_arr[start:end]:
                val = (val << 1) | (int(bit) & 1)
        return val

    def _llc_defrag_add(self, ns: int, ss: int, payload: np.ndarray, final_ss: Optional[int]) -> Optional[np.ndarray]:
        entry = self._llc_defrag.get(ns)
        if entry is None:
            entry = {"segments": {}, "final": None}
            self._llc_defrag[ns] = entry
        segments: Dict[int, np.ndarray] = entry["segments"]  # type: ignore[assignment]
        segments[ss] = np.array(payload, copy=True)
        if final_ss is not None:
            entry["final"] = final_ss

        final = entry["final"]
        if final is None:
            return None
        if not all(idx in segments for idx in range(final + 1)):
            return None
        assembled = np.concatenate([segments[idx] for idx in range(final + 1)])
        del self._llc_defrag[ns]
        return assembled

    def _parse_llc_pdu(self, bits: np.ndarray) -> Optional[np.ndarray]:
        """Parse LLC PDU and return TL-SDU bits if available."""
        if bits is None or len(bits) < 4:
            return None
        if np.any((bits != 0) & (bits != 1)):
            bits = (bits < 0).astype(np.uint8)
        else:
            bits = bits.astype(np.uint8, copy=False)

        def bits_to_uint(start: int, length: int) -> int:
            val = 0
            end = min(start + length, len(bits))
            for bit in bits[start:end]:
                val = (val << 1) | (int(bit) & 1)
            return val

        pdu_type = bits_to_uint(0, 4)
        cur = 4
        tl_sdu_bits = None
        ss = 0
        ns = 0
        final_ss = None

        if pdu_type in (0, 4):  # BL-ADATA / BL-ADATA-FCS
            if cur + 2 > len(bits):
                return None
            cur += 2  # NR + NS
            tl_sdu_bits = bits[cur:]
            if pdu_type == 4 and len(tl_sdu_bits) >= 32:
                tl_sdu_bits = tl_sdu_bits[:-32]
        elif pdu_type in (1, 5):  # BL-DATA / BL-DATA-FCS
            if cur + 1 > len(bits):
                return None
            cur += 1  # NS
            tl_sdu_bits = bits[cur:]
            if pdu_type == 5 and len(tl_sdu_bits) >= 32:
                tl_sdu_bits = tl_sdu_bits[:-32]
        elif pdu_type in (2, 6):  # BL-UDATA / BL-UDATA-FCS
            tl_sdu_bits = bits[cur:]
            if pdu_type == 6 and len(tl_sdu_bits) >= 32:
                tl_sdu_bits = tl_sdu_bits[:-32]
        elif pdu_type in (3, 7):  # BL-ACK / BL-ACK-FCS
            if cur + 1 > len(bits):
                return None
            cur += 1  # NR
            tl_sdu_bits = bits[cur:]
            if pdu_type == 7 and len(tl_sdu_bits) >= 32:
                tl_sdu_bits = tl_sdu_bits[:-32]
        elif pdu_type == 9:  # AL-DATA/FINAL
            if cur + 1 > len(bits):
                return None
            final = bits[cur]
            cur += 1
            if final:
                if cur + 1 + 3 + 8 > len(bits):
                    return None
                cur += 1  # AL_FINAL_AR
                ns = bits_to_uint(cur, 3); cur += 3
                ss = bits_to_uint(cur, 8); cur += 8
                final_ss = ss
                tl_sdu_bits = bits[cur:]
            else:
                if cur + 3 + 8 > len(bits):
                    return None
                ns = bits_to_uint(cur, 3); cur += 3
                ss = bits_to_uint(cur, 8); cur += 8
                tl_sdu_bits = bits[cur:]
        elif pdu_type == 10:  # AL-UDATA/UFINAL
            if cur + 1 > len(bits):
                return None
            final = bits[cur]
            cur += 1
            if cur + 8 + 8 > len(bits):
                return None
            ns = bits_to_uint(cur, 8); cur += 8
            ss = bits_to_uint(cur, 8); cur += 8
            tl_sdu_bits = bits[cur:]
            if final:
                final_ss = ss
        else:
            return None

        if tl_sdu_bits is None or len(tl_sdu_bits) == 0:
            return None
        if pdu_type in (9, 10):
            assembled = self._llc_defrag_add(ns, ss, tl_sdu_bits, final_ss)
            return assembled
        if ss != 0:
            return None
        return tl_sdu_bits

    def _parse_cmce_metadata(self, mac_pdu: MacPDU) -> Optional[CallMetadata]:
        """Parse CMCE call metadata from TM-SDU bits."""
        tm_sdu_bits = mac_pdu.tm_sdu_bits
        if tm_sdu_bits is None or mac_pdu.extra is None:
            return None
        tl_sdu_bits = self._parse_llc_pdu(tm_sdu_bits)
        if tl_sdu_bits is None or len(tl_sdu_bits) < 8:
            return None
        mle_pdisc = self._bits_to_uint(tl_sdu_bits, 0, 3)
        use_lsb = False
        if mle_pdisc != 2:  # TMLE_PDISC_CMCE
            mle_pdisc_lsb = self._bits_to_uint(tl_sdu_bits, 0, 3, lsb=True)
            if mle_pdisc_lsb != 2:
                return None
            use_lsb = True

        cmce_type = self._bits_to_uint(tl_sdu_bits, 3, 5, lsb=use_lsb)
        cmce_type_alt = self._bits_to_uint(tl_sdu_bits, 3, 5, lsb=not use_lsb)
        known_types = set(range(0x00, 0x11))
        if cmce_type not in known_types and cmce_type_alt in known_types:
            cmce_type = cmce_type_alt
            use_lsb = not use_lsb

        cmce_bits = tl_sdu_bits[3:]

        def score_meta(meta: Optional[CallMetadata]) -> int:
            if meta is None:
                return -1
            score = 0
            if meta.source_ssi:
                score += 2
            if meta.dest_ssi:
                score += 2
            if meta.call_identifier is not None:
                score += 1
            if meta.talkgroup_id:
                score += 1
            return score

        def pick_meta(primary: Optional[CallMetadata], alt: Optional[CallMetadata]) -> Optional[CallMetadata]:
            if primary is None:
                return alt
            if alt is None:
                return primary
            return alt if score_meta(alt) > score_meta(primary) else primary

        alt_lsb = not use_lsb

        if cmce_type == 0x07:
            return pick_meta(
                self._parse_cmce_d_setup(cmce_bits, mac_pdu.extra, lsb=use_lsb),
                self._parse_cmce_d_setup(cmce_bits, mac_pdu.extra, lsb=alt_lsb),
            )
        if cmce_type == 0x0B:
            return pick_meta(
                self._parse_cmce_d_tx_granted(cmce_bits, mac_pdu.extra, lsb=use_lsb),
                self._parse_cmce_d_tx_granted(cmce_bits, mac_pdu.extra, lsb=alt_lsb),
            )
        if cmce_type == 0x09:
            return pick_meta(
                self._parse_cmce_d_tx_ceased(cmce_bits, mac_pdu.extra, lsb=use_lsb),
                self._parse_cmce_d_tx_ceased(cmce_bits, mac_pdu.extra, lsb=alt_lsb),
            )
        if cmce_type == 0x08:
            return pick_meta(
                self._parse_cmce_d_status(cmce_bits, mac_pdu.extra, lsb=use_lsb),
                self._parse_cmce_d_status(cmce_bits, mac_pdu.extra, lsb=alt_lsb),
            )
        if cmce_type == 0x06:
            return pick_meta(
                self._parse_cmce_d_release(cmce_bits, mac_pdu.extra, lsb=use_lsb),
                self._parse_cmce_d_release(cmce_bits, mac_pdu.extra, lsb=alt_lsb),
            )
        if cmce_type == 0x02:
            return pick_meta(
                self._parse_cmce_d_connect(cmce_bits, mac_pdu.extra, lsb=use_lsb),
                self._parse_cmce_d_connect(cmce_bits, mac_pdu.extra, lsb=alt_lsb),
            )
        if cmce_type == 0x0F:
            return pick_meta(
                self._parse_cmce_d_sds(cmce_bits, mac_pdu.extra, lsb=use_lsb),
                self._parse_cmce_d_sds(cmce_bits, mac_pdu.extra, lsb=alt_lsb),
            )
        if cmce_type == 0x05:
            return pick_meta(
                self._parse_cmce_d_info(cmce_bits, mac_pdu.extra, lsb=use_lsb),
                self._parse_cmce_d_info(cmce_bits, mac_pdu.extra, lsb=alt_lsb),
            )
        return None

    def _parse_cmce_d_setup(self, bits: np.ndarray, resource: dict, *, lsb: bool = False) -> Optional[CallMetadata]:
        """Parse CMCE D-SETUP message for call metadata."""
        if bits is None or len(bits) < 30:
            return None
        bits_to_uint = lambda start, length: self._bits_to_uint(bits, start, length, lsb=lsb)

        n = 0
        _ = bits_to_uint(n, 5); n += 5  # pdu_type
        call_ident = bits_to_uint(n, 14); n += 14
        _ = bits_to_uint(n, 4); n += 4  # call timeout
        _ = bits_to_uint(n, 1); n += 1  # hook method
        _ = bits_to_uint(n, 1); n += 1  # duplex
        _ = bits_to_uint(n, 8); n += 8  # basic info
        _ = bits_to_uint(n, 2); n += 2  # tx grant
        _ = bits_to_uint(n, 1); n += 1  # tx perm
        _ = bits_to_uint(n, 4); n += 4  # call prio
        if n >= len(bits):
            return None
        o_bit = bits_to_uint(n, 1); n += 1

        calling_ssi = None
        if o_bit:
            if n < len(bits):
                pbit_notif = bits_to_uint(n, 1); n += 1
                if pbit_notif:
                    n += 6
            if n < len(bits):
                pbit_temp = bits_to_uint(n, 1); n += 1
                if pbit_temp:
                    n += 24
            if n < len(bits):
                pbit_cpti = bits_to_uint(n, 1); n += 1
                if pbit_cpti and n + 2 <= len(bits):
                    cpti = bits_to_uint(n, 2); n += 2
                    if cpti == 0 and n + 8 <= len(bits):
                        n += 8
                    elif cpti == 1 and n + 24 <= len(bits):
                        calling_ssi = bits_to_uint(n, 24); n += 24
                    elif cpti == 2 and n + 48 <= len(bits):
                        calling_ssi = bits_to_uint(n, 24); n += 24
                        n += 24  # extension

        talkgroup = self._resource_talkgroup(resource)
        if not self._is_valid_ssi(calling_ssi):
            calling_ssi = None

        return CallMetadata(
            call_type="Group" if talkgroup is not None else "Individual",
            talkgroup_id=talkgroup,
            source_ssi=calling_ssi,
            dest_ssi=None,
            channel_allocated=resource.get("carrier"),
            call_identifier=call_ident,
            call_priority=0,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=resource.get("encryption_mode", 0) > 0,
            encryption_algorithm="TEA1" if resource.get("encryption_mode", 0) > 0 else None,
        )

    def _parse_cmce_d_tx_granted(self, bits: np.ndarray, resource: dict, *, lsb: bool = False) -> Optional[CallMetadata]:
        """Parse CMCE D-TX-GRANTED for caller SSI."""
        if bits is None or len(bits) < 24:
            return None
        bits_to_uint = lambda start, length: self._bits_to_uint(bits, start, length, lsb=lsb)

        n = 0
        _ = bits_to_uint(n, 5); n += 5  # pdu_type
        call_ident = bits_to_uint(n, 14); n += 14
        _ = bits_to_uint(n, 2); n += 2  # tx grant
        _ = bits_to_uint(n, 1); n += 1  # tx perm
        _ = bits_to_uint(n, 1); n += 1  # enc control
        _ = bits_to_uint(n, 1); n += 1  # reserved
        if n >= len(bits):
            return None
        o_bit = bits_to_uint(n, 1); n += 1

        tx_ssi = None
        if o_bit:
            if n < len(bits):
                pbit_nid = bits_to_uint(n, 1); n += 1
                if pbit_nid:
                    n += 6
            if n < len(bits):
                pbit_tpti = bits_to_uint(n, 1); n += 1
                if pbit_tpti and n + 2 <= len(bits):
                    tpti = bits_to_uint(n, 2); n += 2
                    if tpti == 0 and n + 8 <= len(bits):
                        n += 8
                    elif tpti == 1 and n + 24 <= len(bits):
                        tx_ssi = bits_to_uint(n, 24); n += 24
                    elif tpti == 2 and n + 48 <= len(bits):
                        tx_ssi = bits_to_uint(n, 24); n += 24
                        n += 24

        if not self._is_valid_ssi(tx_ssi):
            tx_ssi = None
        talkgroup = self._resource_talkgroup(resource)

        return CallMetadata(
            call_type="Group" if talkgroup is not None else "Individual",
            talkgroup_id=talkgroup,
            source_ssi=tx_ssi,
            dest_ssi=None,
            channel_allocated=resource.get("carrier"),
            call_identifier=call_ident,
            call_priority=0,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=resource.get("encryption_mode", 0) > 0,
            encryption_algorithm="TEA1" if resource.get("encryption_mode", 0) > 0 else None,
        )

    def _parse_cmce_d_sds(self, bits: np.ndarray, resource: dict, *, lsb: bool = False) -> Optional[CallMetadata]:
        """Parse CMCE D-SDS DATA for source/destination SSI."""
        if bits is None or len(bits) < 7:
            return None
        bits_to_uint = lambda start, length: self._bits_to_uint(bits, start, length, lsb=lsb)

        n = 0
        _ = bits_to_uint(n, 5); n += 5  # pdu_type
        cpti = bits_to_uint(n, 2); n += 2
        calling_ssi = None
        if cpti == 0 and n + 8 <= len(bits):
            n += 8
        elif cpti == 1 and n + 24 <= len(bits):
            calling_ssi = bits_to_uint(n, 24)
            n += 24
        elif cpti == 2 and n + 48 <= len(bits):
            calling_ssi = bits_to_uint(n, 24)
            n += 48

        dest_ssi = self._resource_talkgroup(resource)
        if not self._is_valid_ssi(calling_ssi):
            calling_ssi = None
        if not self._is_valid_ssi(dest_ssi):
            dest_ssi = None

        return CallMetadata(
            call_type="SDS",
            talkgroup_id=None,
            source_ssi=calling_ssi,
            dest_ssi=dest_ssi,
            channel_allocated=resource.get("carrier"),
            call_identifier=None,
            call_priority=0,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=resource.get("encryption_mode", 0) > 0,
            encryption_algorithm="TEA1" if resource.get("encryption_mode", 0) > 0 else None,
        )

    def _parse_cmce_d_info(self, bits: np.ndarray, resource: dict, *, lsb: bool = False) -> Optional[CallMetadata]:
        """Parse CMCE D-INFO (best-effort for call identifier)."""
        if bits is None or len(bits) < 19:
            return None
        bits_to_uint = lambda start, length: self._bits_to_uint(bits, start, length, lsb=lsb)

        n = 0
        _ = bits_to_uint(n, 5); n += 5  # pdu_type
        call_ident = bits_to_uint(n, 14)

        talkgroup = self._resource_talkgroup(resource)

        return CallMetadata(
            call_type="Info",
            talkgroup_id=talkgroup,
            source_ssi=None,
            dest_ssi=None,
            channel_allocated=resource.get("carrier"),
            call_identifier=call_ident,
            call_priority=0,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=resource.get("encryption_mode", 0) > 0,
            encryption_algorithm="TEA1" if resource.get("encryption_mode", 0) > 0 else None,
        )

    def _parse_cmce_d_status(self, bits: np.ndarray, resource: dict, *, lsb: bool = False) -> Optional[CallMetadata]:
        """Parse CMCE D-STATUS for calling SSI."""
        if bits is None or len(bits) < 24:
            return None
        bits_to_uint = lambda start, length: self._bits_to_uint(bits, start, length, lsb=lsb)

        n = 0
        _ = bits_to_uint(n, 5); n += 5  # pdu_type
        cpti = bits_to_uint(n, 2); n += 2
        calling_ssi = None
        if cpti == 0 and n + 8 <= len(bits):
            n += 8
        elif cpti == 1 and n + 24 <= len(bits):
            calling_ssi = bits_to_uint(n, 24); n += 24
        elif cpti == 2 and n + 48 <= len(bits):
            calling_ssi = bits_to_uint(n, 24); n += 24
            n += 24

        talkgroup = self._resource_talkgroup(resource)
        if not self._is_valid_ssi(calling_ssi):
            calling_ssi = None

        return CallMetadata(
            call_type="Status",
            talkgroup_id=talkgroup,
            source_ssi=calling_ssi,
            dest_ssi=None,
            channel_allocated=resource.get("carrier"),
            call_identifier=None,
            call_priority=0,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=resource.get("encryption_mode", 0) > 0,
            encryption_algorithm="TEA1" if resource.get("encryption_mode", 0) > 0 else None,
        )

    def _parse_cmce_d_release(self, bits: np.ndarray, resource: dict, *, lsb: bool = False) -> Optional[CallMetadata]:
        """Parse CMCE D-RELEASE for call identifier."""
        if bits is None or len(bits) < 26:
            return None
        bits_to_uint = lambda start, length: self._bits_to_uint(bits, start, length, lsb=lsb)

        n = 0
        _ = bits_to_uint(n, 5); n += 5  # pdu_type
        call_ident = bits_to_uint(n, 14); n += 14

        talkgroup = self._resource_talkgroup(resource)

        return CallMetadata(
            call_type="Release",
            talkgroup_id=talkgroup,
            source_ssi=None,
            dest_ssi=None,
            channel_allocated=resource.get("carrier"),
            call_identifier=call_ident,
            call_priority=0,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=resource.get("encryption_mode", 0) > 0,
            encryption_algorithm="TEA1" if resource.get("encryption_mode", 0) > 0 else None,
        )

    def _parse_cmce_d_connect(self, bits: np.ndarray, resource: dict, *, lsb: bool = False) -> Optional[CallMetadata]:
        """Parse CMCE D-CONNECT for call identifier."""
        if bits is None or len(bits) < 24:
            return None
        bits_to_uint = lambda start, length: self._bits_to_uint(bits, start, length, lsb=lsb)

        n = 0
        _ = bits_to_uint(n, 5); n += 5  # pdu_type
        call_ident = bits_to_uint(n, 14); n += 14

        talkgroup = self._resource_talkgroup(resource)

        return CallMetadata(
            call_type="Connect",
            talkgroup_id=talkgroup,
            source_ssi=None,
            dest_ssi=None,
            channel_allocated=resource.get("carrier"),
            call_identifier=call_ident,
            call_priority=0,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=resource.get("encryption_mode", 0) > 0,
            encryption_algorithm="TEA1" if resource.get("encryption_mode", 0) > 0 else None,
        )

    def _parse_cmce_d_tx_ceased(self, bits: np.ndarray, resource: dict, *, lsb: bool = False) -> Optional[CallMetadata]:
        """Parse CMCE D-TX-CEASED (best-effort, similar to D-TX-GRANTED)."""
        return self._parse_cmce_d_tx_granted(bits, resource, lsb=lsb)

    def parse_call_metadata(self, mac_pdu: MacPDU) -> Optional[CallMetadata]:
        """
        Extract call metadata from MAC PDU (talkgroup, SSI, etc.).
        
        Args:
            mac_pdu: MAC PDU to parse
            
        Returns:
            CallMetadata or None
        """
        if not mac_pdu:
            return None

        if mac_pdu.tm_sdu_bits is not None and mac_pdu.extra:
            cmce_meta = self._parse_cmce_metadata(mac_pdu)
            if cmce_meta:
                if cmce_meta.source_ssi:
                    self._ssi_votes[cmce_meta.source_ssi] += 1
                if cmce_meta.dest_ssi:
                    self._ssi_votes[cmce_meta.dest_ssi] += 1
                if mac_pdu.crc_ok is False:
                    ssi_values = [v for v in (cmce_meta.source_ssi, cmce_meta.dest_ssi) if v]
                    if not ssi_values:
                        return None
                    if not any(self._ssi_votes[v] >= 2 for v in ssi_values):
                        return None
                return cmce_meta

        if mac_pdu.crc_ok is False:
            return None

        if mac_pdu.encrypted:
            return None

        # Parse based on PDU type (fallback)
        if mac_pdu.pdu_type == PDUType.MAC_RESOURCE:
            return self._parse_resource_assignment(mac_pdu)
        if mac_pdu.pdu_type == PDUType.MAC_U_SIGNAL:
            return self._parse_call_setup(mac_pdu)
        if mac_pdu.pdu_type == PDUType.MAC_BROADCAST:
            return self._parse_broadcast(mac_pdu)

        return None
    
    def _parse_resource_assignment(self, mac_pdu: MacPDU) -> Optional[CallMetadata]:
        """Parse resource assignment message."""
        data = mac_pdu.data
        if len(data) < 8:
            return None

        call_type = "Group" if data[0] & 0x80 else "Individual"

        talkgroup_id = None
        source_ssi = None
        if mac_pdu.extra:
            talkgroup_id = self._resource_talkgroup(mac_pdu.extra)
        if talkgroup_id is None and self._is_valid_ssi(mac_pdu.address):
            talkgroup_id = mac_pdu.address

        channel_allocated = None
        if mac_pdu.extra:
            channel_allocated = mac_pdu.extra.get("carrier")

        encryption_enabled = False
        if mac_pdu.extra and mac_pdu.extra.get("encryption_mode"):
            encryption_enabled = mac_pdu.extra.get("encryption_mode", 0) > 0
        call_priority = (data[5] >> 2) & 0x0F if len(data) > 5 else 0

        call_identifier = None
        if len(data) > 7:
            call_identifier = ((data[6] & 0x0F) << 10) | (data[7] << 2)
        
        self.stats['control_messages'] += 1
        
        return CallMetadata(
            call_type=call_type,
            talkgroup_id=talkgroup_id,
            source_ssi=source_ssi,
            dest_ssi=None,
            channel_allocated=channel_allocated,
            call_identifier=call_identifier,
            call_priority=call_priority,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=encryption_enabled,
            encryption_algorithm="TEA1" if encryption_enabled else None
        )
    
    def _parse_call_setup(self, mac_pdu: MacPDU) -> Optional[CallMetadata]:
        """Parse call setup signaling."""
        data = mac_pdu.data
        if len(data) < 12:
            return None
        
        # Extract SSIs
        source_ssi = int.from_bytes(data[0:3], 'big') & 0xFFFFFF
        dest_ssi = int.from_bytes(data[3:6], 'big') & 0xFFFFFF
        if not self._is_valid_ssi(source_ssi):
            source_ssi = None
        if not self._is_valid_ssi(dest_ssi):
            dest_ssi = None
        
        # Call type
        call_type_byte = data[6]
        if call_type_byte & 0x80:
            call_type = "Voice"
            self.stats['voice_calls'] += 1
        else:
            call_type = "Data"
            self.stats['data_messages'] += 1
        
        # Encryption
        encryption_enabled = bool(data[7] & 0x80)
        encryption_alg = None
        if encryption_enabled:
            alg_code = (data[7] >> 4) & 0x07
            if alg_code == 1:
                encryption_alg = "TEA1"
            elif alg_code == 2:
                encryption_alg = "TEA2"
            elif alg_code == 3:
                encryption_alg = "TEA3"
            elif alg_code == 4:
                encryption_alg = "TEA4"
        
        return CallMetadata(
            call_type=call_type,
            talkgroup_id=dest_ssi if call_type == "Voice" and dest_ssi is not None else None,
            source_ssi=source_ssi,
            dest_ssi=dest_ssi,
            channel_allocated=None,
            call_identifier=None,
            call_priority=0,
            mcc=self.mcc,
            mnc=self.mnc,
            encryption_enabled=encryption_enabled,
            encryption_algorithm=encryption_alg
        )

    def _parse_broadcast(self, mac_pdu: MacPDU) -> Optional[CallMetadata]:
        """
        Parse MAC-BROADCAST metadata.

        MCC/MNC/CC are authoritative from SYNC SB1 (lower MAC). SYSINFO
        contributes Location Area via mac_pdu.extra['location_area'].
        """
        try:
            mcc = self.mcc
            mnc = self.mnc
            colour_code = self.colour_code or 0
            la = None
            if mac_pdu.extra:
                la = mac_pdu.extra.get("location_area")
                if la is not None:
                    self.la = la

            # Prefer already-locked cell identity from SB1
            if mcc is None or mnc is None:
                data = mac_pdu.data
                if data is None or len(data) < 4:
                    return None
                # Legacy fallback only if SB1 has not locked yet
                bits = BitArray(data)
                if len(bits) >= 30:
                    cand_mcc = bits[0:10].uint
                    cand_mnc = bits[10:24].uint
                    cand_cc = bits[24:30].uint
                    if 200 <= cand_mcc <= 799 and cand_mnc <= 999:
                        mcc, mnc, colour_code = cand_mcc, cand_mnc, cand_cc
                        self._record_network_candidate(
                            mcc, mnc, colour_code, strong=mac_pdu.crc_ok is True
                        )

            if mcc is None or mnc is None:
                return None

            if mac_pdu.crc_ok is True or (mcc is not None and mnc is not None):
                logger.info(
                    "Decoded TETRA network: MCC=%s MNC=%s CC=%s LA=%s",
                    mcc,
                    mnc,
                    colour_code,
                    la if la is not None else self.la,
                )

            return CallMetadata(
                call_type="Broadcast",
                talkgroup_id=None,
                source_ssi=None,
                dest_ssi=None,
                channel_allocated=None,
                mcc=mcc,
                mnc=mnc,
                encryption_enabled=False,
            )
        except Exception:
            return None
    
    def parse_sds_message(self, mac_pdu: MacPDU) -> Optional[str]:
        """
        Parse Short Data Service (SDS) text message.
        
        Args:
            mac_pdu: MAC PDU containing SDS
            
        Returns:
            Decoded text message or None
        """
        if mac_pdu.pdu_type != PDUType.MAC_DATA and mac_pdu.pdu_type != PDUType.MAC_SUPPL:
            return None
        
        # SDS data is in the payload
        return self.parse_sds_data(mac_pdu.data)

    def parse_sds_data(self, data: bytes) -> Optional[str]:
        """
        Parse SDS data payload based on Protocol Identifier (PID) or heuristics.
        Supports SDS-1 (Text), SDS-TL (PID), and GSM 7-bit encoding.
        
        Args:
            data: Raw data bytes
            
        Returns:
            Decoded text string or None
        """
        if not data or len(data) < 1:
            return None
        
        # Strip trailing null bytes for text detection
        data_stripped = data.rstrip(b'\x00')
        if not data_stripped:
            return None
            
        # --- Check for User-Defined SDS Types (based on user examples) ---
        # Example 1: SDS-1 Text (05 00 Length ...)
        if len(data) > 3 and data[0] == 0x05 and data[1] == 0x00:
            # User example: 05 00 C8 48 45 4C 4C 4F -> HELLO
            # Payload starts at offset 3
            payload = data[3:].rstrip(b'\x00')
            try:
                text = payload.decode('ascii')
                if self._is_valid_text(text):
                    self.stats['data_messages'] += 1
                    return f"[SDS-1] {text}"
            except:
                pass

        # Example 2: SDS with GSM 7-bit (07 00 Length ...)
        if len(data) > 3 and data[0] == 0x07 and data[1] == 0x00:
            # User example: 07 00 D2 D4 79 9E 2F 03 -> STATUS OK
            candidates: List[str] = []

            # Some SDS payloads include a septet count at offset 2.
            septet_count = data[2]
            payload_3 = data[3:]
            if payload_3:
                max_septets = (len(payload_3) * 8) // 7
                if 0 < septet_count <= min(160, max_septets):
                    candidates.append(self._unpack_gsm7bit(payload_3, septet_count=septet_count))
                    candidates.append(self._unpack_gsm7bit_with_udh(payload_3, septet_count=septet_count))
                candidates.append(self._unpack_gsm7bit(payload_3))
                candidates.append(self._unpack_gsm7bit_with_udh(payload_3))

            # Fallback: decode starting at offset 2 (treat offset-2 byte as packed content).
            payload_2 = data[2:]
            if payload_2:
                candidates.append(self._unpack_gsm7bit(payload_2))
                candidates.append(self._unpack_gsm7bit_with_udh(payload_2))

            best = ""
            best_score = 0.0
            seen = set()
            for text in candidates:
                text = text.strip("\x00").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                s = self._score_text(text)
                if s > best_score:
                    best_score = s
                    best = text

            if best and self._is_valid_text(best, threshold=0.55):
                self.stats['data_messages'] += 1
                return f"[SDS-GSM] {best}"

        # --- Standard SDS-TL PID Checks ---
        pid = data[0]
        payload = data[1:].rstrip(b'\x00')
        
        if pid == 0x82:  # Text Messaging (ISO 8859-1)
            try:
                text = payload.decode('latin-1')
                if self._is_valid_text(text):
                    self.stats['data_messages'] += 1
                    return f"[TXT] {text}"
            except:
                pass
                
        elif pid == 0x03:  # Simple Text Messaging (ASCII)
            try:
                text = payload.decode('ascii')
                if self._is_valid_text(text):
                    self.stats['data_messages'] += 1
                    return f"[TXT] {text}"
            except:
                pass
            
        elif pid == 0x83:  # Location
            # Try to parse LIP
            lip_text = self.parse_lip(payload)
            if lip_text:
                return f"[LIP] {lip_text}"
            return f"[LOC] Location Data: {payload.hex()}"
            
        elif pid == 0x0C:  # GPS
            # Try to parse LIP (PID 0x0C is often used for LIP too)
            lip_text = self.parse_lip(payload)
            if lip_text:
                return f"[LIP] {lip_text}"
            return f"[GPS] GPS Data: {payload.hex()}"
            
        # --- Fallback Heuristics ---
        
        # Use stripped data for text detection
        test_data = data_stripped
        
        # Check for 7-bit GSM packing or 8-bit text
        # Heuristic: if > 60% of bytes are printable, treat as text
        printable_count = sum(1 for b in test_data if 32 <= b <= 126 or b in (10, 13))
        if len(test_data) > 0 and (printable_count / len(test_data)) > 0.6:
             try:
                # Try multiple encodings
                text = None
                for encoding in ['utf-8', 'latin-1', 'ascii', 'cp1252']:
                    try:
                        text = test_data.decode(encoding, errors='strict')
                        if self._is_valid_text(text, threshold=0.6):
                            self.stats['data_messages'] += 1
                            return f"[TXT] {text}"
                    except:
                        continue
                
                # If strict decode failed, try with errors='replace'
                if not text:
                    text = test_data.decode('latin-1', errors='replace')
                    if self._is_valid_text(text, threshold=0.6):
                        self.stats['data_messages'] += 1
                        return f"[TXT] {text}"
             except:
                pass
        
        # Try GSM 7-bit unpacking as last resort (with UDH handling)
        try:
            candidates = [
                self._unpack_gsm7bit(test_data),
                self._unpack_gsm7bit_with_udh(test_data),
            ]
            best = ""
            best_score = 0.0
            seen = set()
            for text in candidates:
                text = text.strip("\x00").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                score = self._score_text(text)
                if score > best_score:
                    best_score = score
                    best = text
            if best and self._is_valid_text(best, threshold=0.55):
                self.stats['data_messages'] += 1
                return f"[GSM7] {best}"
        except Exception:
            pass
        
        # Check for Encrypted Binary SDS (High Entropy)
        if len(test_data) > 8:
            unique_bytes = len(set(test_data))
            entropy_ratio = unique_bytes / len(test_data)
            if entropy_ratio > 0.7:
                # Show hex dump for analysis
                hex_preview = test_data[:32].hex(' ').upper()
                if len(test_data) > 32:
                    hex_preview += "..."
                return f"[BIN-ENC] SDS (Binary/Encrypted) - {len(test_data)} bytes | {hex_preview}"

        # Default to Hex dump for binary data
        def hex_preview(buf: bytes, max_bytes: int = 48) -> str:
            if len(buf) <= max_bytes:
                return buf.hex(" ").upper()
            return buf[:max_bytes].hex(" ").upper() + " ..."

        pid = data_stripped[0]
        payload = data_stripped[1:]

        parts = [f"PID=0x{pid:02X}", f"HEX={hex_preview(data_stripped, max_bytes=32)}"]

        if payload:
            printable_count = sum(1 for b in payload if 32 <= b <= 126 or b in (10, 13, 9))
            if (printable_count / len(payload)) >= 0.85:
                try:
                    ascii_text = payload.decode("latin-1", errors="replace").replace("\r", "").replace("\x00", "")
                    ascii_text = "".join(c for c in ascii_text if c.isprintable() or c in "\n\t").strip()
                    if ascii_text:
                        parts.append(f"ASCII=\"{ascii_text[:60]}\"")
                except Exception:
                    pass

            tlv_items = []
            idx = 0
            while idx + 2 <= len(payload):
                tag = payload[idx]
                length = payload[idx + 1]
                if length == 0 or idx + 2 + length > len(payload):
                    break
                value = payload[idx + 2: idx + 2 + length]
                tlv_items.append(f"{tag:02X}:{length}={hex_preview(value, max_bytes=12)}")
                idx += 2 + length
                if len(tlv_items) >= 4:
                    break
            if tlv_items and idx >= max(3, int(len(payload) * 0.75)):
                parts.append("TLV=" + " ".join(tlv_items))

            if len(payload) in (2, 4, 6, 8, 10, 12) and len(payload) <= 12:
                words_le = [int.from_bytes(payload[i:i + 2], "little") for i in range(0, len(payload), 2)]
                words_be = [int.from_bytes(payload[i:i + 2], "big") for i in range(0, len(payload), 2)]
                parts.append("u16le=" + ",".join(f"0x{w:04X}" for w in words_le))
                parts.append("u16be=" + ",".join(f"0x{w:04X}" for w in words_be))

        return "[BIN] " + " | ".join(parts)

    def parse_lip(self, data: bytes) -> Optional[str]:
        """
        Parse Location Information Protocol (LIP) payload.
        ETSI TS 100 392-18-1.
        Handles Basic Location Report (Short/Long).
        """
        if not data or len(data) < 2:
            return None
            
        try:
            # LIP PDU Type (first 2 bits)
            # 00: Short Location Report
            # 01: Long Location Report
            # 10: Location Report with Ack
            # 11: Reserved/Extended
            
            # Convert to bits for easier parsing
            bits = BitArray(data)
            pdu_type = bits[0:2].uint
            
            if pdu_type == 0: # Short Location Report
                # Structure: Type(2), Time Elapsed(2), Lat(24), Long(25), Pos Error(3), Horizontal Vel(5), Direction(4)
                # Total ~65 bits
                if len(bits) < 65:
                    return None
                    
                # Time Elapsed (0-3) - 0=Current, 1=<5s, 2=<5min, 3=>5min
                time_elapsed = bits[2:4].uint
                
                # Latitude (24 bits, 2's complement)
                lat_raw = bits[4:28].int
                # Scaling: lat_raw * 90 / 2^23
                latitude = lat_raw * 90.0 / (1 << 23)
                
                # Longitude (25 bits, 2's complement)
                lon_raw = bits[28:53].int
                # Scaling: lon_raw * 180 / 2^24
                longitude = lon_raw * 180.0 / (1 << 24)
                
                return f"Lat: {latitude:.5f}, Lon: {longitude:.5f} (Short)"
                
            elif pdu_type == 1: # Long Location Report
                # Structure: Type(2), Time Elapsed(2), Lat(25), Long(26), Pos Error(3), Horizontal Vel(8), Direction(9)
                # Total ~75 bits
                if len(bits) < 75:
                    return None
                    
                # Latitude (25 bits)
                lat_raw = bits[4:29].int
                latitude = lat_raw * 90.0 / (1 << 24)
                
                # Longitude (26 bits)
                lon_raw = bits[29:55].int
                longitude = lon_raw * 180.0 / (1 << 25)
                
                return f"Lat: {latitude:.5f}, Lon: {longitude:.5f} (Long)"
                
            # Heuristic for raw NMEA (sometimes sent as text in LIP PID)
            try:
                text = data.decode('ascii')
                if "$GPGGA" in text or "$GPRMC" in text:
                    return f"NMEA: {text.strip()}"
            except:
                pass
                
        except Exception as e:
            logger.debug(f"LIP parsing error: {e}")
            
        return None

    _GSM7_DEFAULT_ALPHABET = [
        "@", "£", "$", "¥", "è", "é", "ù", "ì", "ò", "Ç", "\n", "Ø", "ø", "\r", "Å", "å",
        "Δ", "_", "Φ", "Γ", "Λ", "Ω", "Π", "Ψ", "Σ", "Θ", "Ξ", "\x1b", "Æ", "æ", "ß", "É",
        " ", "!", "\"", "#", "¤", "%", "&", "'", "(", ")", "*", "+", ",", "-", ".", "/",
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ":", ";", "<", "=", ">", "?",
        "¡", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O",
        "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "Ä", "Ö", "Ñ", "Ü", "§",
        "¿", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o",
        "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "ä", "ö", "ñ", "ü", "à",
    ]

    _GSM7_EXTENSION_TABLE = {
        0x0A: "\f",
        0x14: "^",
        0x28: "{",
        0x29: "}",
        0x2F: "\\",
        0x3C: "[",
        0x3D: "~",
        0x3E: "]",
        0x40: "|",
        0x65: "€",
    }

    def _unpack_gsm7bit(
        self,
        data: bytes,
        septet_count: Optional[int] = None,
        skip_bits: int = 0,
    ) -> str:
        """
        Unpack GSM 03.38 7-bit packed data into text.

        Args:
            data: Packed septets (octet stream)
            septet_count: Optional number of septets to decode
            skip_bits: Number of leading bits to skip (for UDH alignment)
        """
        if not data:
            return ""

        bits: List[int] = []
        for b in data:
            for i in range(8):
                bits.append((b >> i) & 1)

        if skip_bits:
            if skip_bits >= len(bits):
                return ""
            bits = bits[skip_bits:]

        max_septets = len(bits) // 7
        if septet_count is None or septet_count > max_septets:
            septet_count = max_septets

        septets: List[int] = []
        for idx in range(septet_count):
            base = idx * 7
            val = 0
            for offset in range(7):
                val |= (bits[base + offset] << offset)
            septets.append(val)

        out: List[str] = []
        escaped = False
        for code in septets:
            if escaped:
                out.append(self._GSM7_EXTENSION_TABLE.get(code, ""))
                escaped = False
                continue
            if code == 0x1B:
                escaped = True
                continue
            out.append(self._gsm_map(code))

        return "".join(out)

    def _unpack_gsm7bit_with_udh(self, data: bytes, septet_count: Optional[int] = None) -> str:
        """
        Unpack GSM 03.38 7-bit packed data with UDH handling.

        The first octet is treated as UDHL when it yields a plausible header length.
        """
        if not data or len(data) < 2:
            return ""

        udh_len = data[0]
        if udh_len <= 0:
            return ""

        udh_total = udh_len + 1
        if udh_total > len(data):
            return ""

        skip_bits = udh_total * 8
        payload_septets = None
        if septet_count is not None:
            udh_septets = (skip_bits + 6) // 7
            if septet_count > udh_septets:
                payload_septets = septet_count - udh_septets

        return self._unpack_gsm7bit(
            data,
            septet_count=payload_septets,
            skip_bits=skip_bits,
        )

    def _gsm_map(self, code: int) -> str:
        """Map GSM 03.38 default-alphabet code to character."""
        if 0 <= code < len(self._GSM7_DEFAULT_ALPHABET):
            ch = self._GSM7_DEFAULT_ALPHABET[code]
            return "" if ch == "\x1b" else ch
        return ""

    def _score_text(self, text: str) -> float:
        """Score decoded text to select the most plausible candidate."""
        if not text:
            return 0.0
        printable = sum(1 for c in text if c.isprintable() and c not in "\x1b")
        alnum = sum(1 for c in text if c.isalnum() or c.isspace())
        alpha = sum(1 for c in text if c.isalpha())
        return (printable / len(text)) + (alnum / len(text)) + (0.5 if alpha > 0 else 0.0)

    def _is_valid_text(self, text: str, threshold: float = 0.8) -> bool:
        """Check if string looks like valid human-readable text."""
        if not text or len(text) < 2:
            return False
            
        # Remove common whitespace
        clean_text = ''.join(c for c in text if c not in '\n\r\t ')
        if not clean_text:
            return False
            
        # Check ratio of printable characters
        printable = sum(1 for c in text if c.isprintable() or c in '\n\r\t')
        ratio = printable / len(text)
        
        # Check for excessive repetition (padding)
        if len(text) > 4 and text.count(text[0]) == len(text):
            return False
            
        # Check for high density of symbols (binary data often looks like symbols)
        alnum = sum(1 for c in text if c.isalnum() or c == ' ')
        alnum_ratio = alnum / len(text)
        
        return ratio >= threshold and alnum_ratio > 0.5



    def extract_voice_payload(self, mac_pdu: MacPDU) -> Optional[bytes]:
        """
        Extract ACELP voice payload from MAC PDU.
        
        Args:
            mac_pdu: MAC PDU
            
        Returns:
            Voice payload bytes or None
        """
        # Voice is usually in MAC-TRAFFIC (which maps to specific burst types)
        # But here we might receive it as MAC_U_SIGNAL or similar if not parsed correctly
        # In TETRA, voice frames are typically 2 slots interleaved
        
        # For this implementation, we assume the payload IS the voice frame
        # if the frame type indicates traffic
        
        if not mac_pdu.data:
            return None
            
        return mac_pdu.data
    
    def get_statistics(self) -> Dict:
        """Get parsing statistics."""
        total = self.stats['clear_mode_frames'] + self.stats['encrypted_frames']
        if total > 0:
            clear_pct = (self.stats['clear_mode_frames'] / total) * 100
            enc_pct = (self.stats['encrypted_frames'] / total) * 100
        else:
            clear_pct = enc_pct = 0
        
        return {
            **self.stats,
            'clear_mode_percentage': clear_pct,
            'encrypted_percentage': enc_pct,
            'crc_success_rate': (self.stats['crc_pass'] / max(1, self.stats['total_bursts'])) * 100
        }
    
    def format_call_metadata(self, metadata: CallMetadata) -> str:
        """Format call metadata for display."""
        lines = [
            f"📞 Call Type: {metadata.call_type}",
        ]
        
        if metadata.talkgroup_id:
            lines.append(f"👥 Talkgroup: {metadata.talkgroup_id}")
        
        if metadata.source_ssi:
            lines.append(f"📱 Source SSI: {metadata.source_ssi}")
        
        if metadata.dest_ssi:
            lines.append(f"📱 Dest SSI: {metadata.dest_ssi}")
        
        if metadata.channel_allocated:
            lines.append(f"📡 Channel: {metadata.channel_allocated}")
        
        if metadata.encryption_enabled:
            lines.append(f"🔒 Encryption: {metadata.encryption_algorithm or 'Unknown'}")
        else:
            lines.append("🔓 Clear Mode (No Encryption)")
        
        return "\n".join(lines)
