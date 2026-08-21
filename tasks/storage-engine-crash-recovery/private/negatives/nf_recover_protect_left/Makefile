# ministore - build
CC      ?= cc
CFLAGS  ?= -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -g -Wall -Wextra -Iinclude -Isrc
LDFLAGS ?=

SRC  = src/util.c src/pager.c src/wal.c src/node.c src/btree.c \
       src/recover.c src/kvstore.c
OBJ  = $(SRC:.c=.o)
LIB  = build/libministore.a
BIN  = build/kvcli

.PHONY: all clean test check

all: $(BIN)

$(LIB): $(OBJ)
	@mkdir -p build
	ar rcs $@ $(OBJ)

$(BIN): tools/kvcli.c $(LIB)
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ tools/kvcli.c $(LIB) $(LDFLAGS)

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

test check: all
	@tests/run_all.sh

clean:
	rm -f $(OBJ)
	rm -rf build
