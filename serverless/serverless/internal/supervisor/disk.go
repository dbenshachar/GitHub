package supervisor

import (
	"encoding/binary"
	"os"
)

// Fresh sparse FAT32 volume. Only metadata is written; no guest state is cloned.
func blankDisk(path string) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	if err = f.Truncate(16 * 1024 * 1024); err != nil {
		return err
	}
	fat := uint32(1)
	for {
		n := ((32768-32-2*fat+2)*4 + 511) / 512
		if n == fat {
			break
		}
		fat = n
	}
	b := make([]byte, 512)
	put16 := func(off int, v uint16) { binary.LittleEndian.PutUint16(b[off:], v) }
	put32 := func(off int, v uint32) { binary.LittleEndian.PutUint32(b[off:], v) }
	copy(b, []byte{0xeb, 0x58, 0x90})
	copy(b[3:], "MINIOS  ")
	put16(11, 512)
	b[13] = 1
	put16(14, 32)
	b[16] = 2
	b[21] = 0xf8
	put16(24, 63)
	put16(26, 255)
	put32(32, 32768)
	put32(36, fat)
	put32(44, 2)
	put16(48, 1)
	put16(50, 6)
	b[64] = 0x80
	b[66] = 0x29
	put32(67, 0x20260409)
	copy(b[71:], "MINI_OS    ")
	copy(b[82:], "FAT32   ")
	b[510] = 0x55
	b[511] = 0xaa
	if _, err = f.WriteAt(b, 0); err != nil {
		return err
	}
	if _, err = f.WriteAt(b, 6*512); err != nil {
		return err
	}
	clear(b)
	put32(0, 0x41615252)
	put32(484, 0x61417272)
	put32(488, 32768-32-2*fat-1)
	put32(492, 3)
	put16(510, 0xaa55)
	if _, err = f.WriteAt(b, 512); err != nil {
		return err
	}
	clear(b)
	put32(0, 0x0ffffff8)
	put32(4, 0xffffffff)
	put32(8, 0x0fffffff)
	for i := uint32(0); i < 2; i++ {
		if _, err = f.WriteAt(b, int64(32+i*fat)*512); err != nil {
			return err
		}
	}
	return nil
}
