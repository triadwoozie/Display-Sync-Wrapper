CC ?= gcc
CFLAGS ?= -Wall -Wextra -O2 $(shell pkg-config --cflags libevdev)
LDFLAGS ?= $(shell pkg-config --libs libevdev)

all: intercept

intercept: intercept.c
	$(CC) $(CFLAGS) $< $(LDFLAGS) -o $@

clean:
	rm -f intercept

.PHONY: all clean
