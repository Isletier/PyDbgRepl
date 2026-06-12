# External references

Background reading behind the design decisions in `doc/io_model.md` and the
pty/termios handling in `src/commands/_internal.py`.

- W. Richard Stevens, Stephen A. Rago, *Advanced Programming in the UNIX
  Environment*, 3rd Edition (Addison-Wesley, ISBN 0321637739).
  https://raw.githubusercontent.com/zwan074/technical-books/master/Advanced.Programming.in.the.UNIX.Environment.3rd.Edition.0321637739.pdf
  Pty/termios/line-discipline internals (the chapter on pseudo terminals)
  underpin `_StdinPassthrough`'s use of cbreak mode.

- Don Libes, on `expect` and programmatically driving interactive programs
  via ptys (NIST technical report).
  https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=821311
  The "wait for output, then send input" pattern this describes is the
  rationale for not trying to detect "the inferior is blocked on stdin" at
  the OS level (see the discussion in `doc/io_model.md`).
