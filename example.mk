.PHONY: all install clean

all: executable
	@echo done

executable: a.o b.o c.o d.o
	@echo gcc a.o b.o c.o d.o -o executable
	@sleep 1
	touch $@

%.o:
	@echo gcc -c $< -o $@
	@sleep 1
	touch $@

install: executable
	@echo mkdir -p /usr/local/bin
	@echo install -m 755 executable /usr/local/bin/executable
	@sleep 1

clean:
	@rm -f a.o b.o c.o d.o executable