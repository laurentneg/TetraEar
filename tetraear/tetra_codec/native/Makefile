# Build shared channel decoder from patched ETSI sources + façade.
# Linux: libtetra_cdec.so  |  Windows (MinGW): tetra_cdec.dll
CC      ?= gcc
CFLAGS  ?= -O3 -Wall -fPIC -I. -Ietsi_cdec
LDFLAGS ?= -shared

SRCS = tetra_cdec.c etsi_cdec/cdec_tet.c etsi_cdec/sub_cd.c etsi_cdec/tetra_op.c
OBJS = $(SRCS:.c=.o)

LIBDIR  ?= ../lib

ifeq ($(OS),Windows_NT)
TARGET  = $(LIBDIR)/tetra_cdec.dll
CFLAGS += -DTETRA_CDEC_BUILDING
else
TARGET  = $(LIBDIR)/libtetra_cdec.so
endif

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(OBJS) | $(LIBDIR)
	$(CC) $(LDFLAGS) -o $@ $(OBJS)

$(LIBDIR):
	mkdir -p $(LIBDIR)

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

etsi_cdec/%.o: etsi_cdec/%.c
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f $(OBJS) $(TARGET) $(LIBDIR)/tetra_cdec.dll $(LIBDIR)/libtetra_cdec.so
