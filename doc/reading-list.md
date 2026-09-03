# Reading list: decoupled input loops, namespaces, and modes

Annotated bibliography from a session-long discussion about decoupling a
shell/REPL's input loop from a program's rendering, and the deeper question
of how much interpretation of user input a program needs to own for itself.
Compiled 2026-09-03 across four parallel research passes, each instructed to
chase citation trails through primary sources rather than work from memory.
Also published as an artifact: https://claude.ai/code/artifact/ec536cc2-870c-4c3c-9c29-e1c2983c18e9

Items marked **(unverified)** were corroborated by at least one secondary
bibliography but a live link or exact venue detail couldn't be independently
confirmed this pass.

## 1. Modes, direct manipulation, and what a captured input loop costs

- Ben Shneiderman, "Direct Manipulation: A Step Beyond Programming
  Languages", *Computer* (IEEE) 16(8), 1983, pp. 57-69.
  https://www.semanticscholar.org/paper/Direct-Manipulation:-A-Step-Beyond-Programming-Shneiderman/50d0956efba1532c370ef56f605dba5defffe91e
  Coined the term. Argues visible objects, reversible actions, and rapid
  feedback beat command languages for most tasks - the strongest existing
  case against shell sovereignty.

- Don Norman, "Design Rules Based on Analyses of Human Error",
  *Communications of the ACM* 26(4), 1983, pp. 254-258.
  https://simson.net/ref/1983/norman83.pdf
  Defines the "slip": correct intention, wrong action, because interface
  state silently changed what an action meant.

- Jeff Johnson & George Engelbeck, "Modes Survey Results", *SIGCHI
  Bulletin* 20(4), 1989, pp. 38-50.
  https://dl.acm.org/doi/pdf/10.1145/67243.67248
  Surveyed practitioners on what they mean by "mode" - the field never
  agreed modes are simply avoidable, only that they need signaling.

- Harold Thimbleby, *User Interface Design*, Addison-Wesley / ACM Press,
  1990. https://archive.org/details/userinterfacedes00thim
  A mode changes what an action *means*; a context only changes which
  actions are *available*. Only the former is dangerous.

- Harold Thimbleby, *Press On: Principles of Interaction Programming*,
  MIT Press, 2007.
  Formalizes interfaces as state machines to make mode-errors provable
  rather than anecdotal.

- "A Comparative Study of Moded and Modeless Text Editing by Experienced
  Editor Users", Proceedings of CHI '83, ACM.
  https://dl.acm.org/doi/10.1145/800045.801603
  An actual controlled user study, not just a design argument.

## 2. Tesler and the birth of modeless editing

- Larry Tesler, "A Personal History of Modeless Text Editing and
  Cut/Copy-Paste", *Interactions* 19(4), 2012, pp. 70-75.
  https://worrydream.com/refs/Tesler_2012_-_A_Personal_History_of_Modeless_Text_Editing_and_Cut-Copy-Paste.pdf
  His own CHI-award writeup of building Gypsy at PARC.

- Larry Tesler, "How Modeless Editing Came To Be", *IEEE Annals of the
  History of Computing* 40(3), 2018, pp. 55-67.
  https://muse.jhu.edu/article/715606/pdf
  A longer, more detailed retelling six years on.

- "Gypsy (software)", Wikipedia. https://en.wikipedia.org/wiki/Gypsy_(software)
  Also: Computer History Museum demo recording
  (https://www.computerhistory.org/collections/catalog/102738551) and a
  Digital Seams retrospective
  (https://digitalseams.com/blog/the-gypsy-document-editor-celebrating-50-years).
  Tesler & Tim Mott's actual 1974-76 editor, the direct ancestor of
  cut/copy/paste.

## 3. Raskin's modeless crusade

- Jef Raskin, *The Humane Interface: New Directions for Designing
  Interactive Systems*, Addison-Wesley, 2000.
  https://archive.org/details/humaneinterfacen00rask
  A mode is where a program captures interpretation of your input -
  treated as a non-negotiable design constraint.

- "Archy / The Humane Environment", Wikipedia.
  https://en.wikipedia.org/wiki/Archy_(software)
  Also: Raskin Center for Humane Interfaces (https://raskincenter.org/rchi/)
  and the original SourceForge project
  (https://sourceforge.net/projects/humane/).
  Open-sourced in Java after Raskin's 2005 death; the continuation effort
  was later redirected into a Firefox extension rather than kept as Archy.

## 4. Plan 9: everything is a namespace

- Pike, Presotto, Dorward, Flandrena, Thompson, Trickey, Winterbottom,
  "Plan 9 from Bell Labs", *Computing Systems* 8(3), 1995, pp. 221-254.
  https://css.csail.mit.edu/6.824/2014/papers/plan9.pdf
  Kernel as a 9P multiplexer, per-process namespaces. Its own Discussion
  section admits the file metaphor "can be abused."

- Pike, Presotto, Thompson, Trickey, Winterbottom, "The Use of Name
  Spaces in Plan 9", *ACM Operating Systems Review* 27(2), 1993, pp. 72-76.
  https://9p.io/sys/doc/names.pdf
  The bind/mount paper - lets a shell splice an app's control surface
  into its own view with no negotiated API.

- Rob Pike, "8 1/2, the Plan 9 Window System", USENIX Summer Conference
  Proceedings, Nashville, 1991, pp. 257-265.
  `rio`'s predecessor. Windows-as-files.

- Rob Pike, "Acme: A User Interface for Programmers", USENIX Winter
  Conference Proceedings, San Francisco, 1994, pp. 223-234.
  https://www.usenix.org/legacy/publications/library/proceedings/sf94/full_papers/pike.pdf
  (mirror: https://research.swtch.com/acme.pdf)
  "A hybrid of window system, shell, and editor" with no plugin API -
  everything is files.

- Rob Pike, "Plumbing and Other Utilities", Bell Labs.
  https://9p.io/sys/doc/plumb.pdf
  The plumber as a language-driven file server for routing text between
  programs, contrasted with embedding a command interpreter in every app.

- Rob Pike, "The Text Editor sam", *Software-Practice and Experience*
  17(11), 1987, pp. 813-845.
  sam's structural-regexp, externally-scriptable command language is the
  direct ancestor of Acme.

- Rob Pike, "Structural Regular Expressions", EUUG Spring Conference
  Proceedings, Helsinki, 1987, pp. 21-28.
  The theoretical basis under sam and Acme's addressing language.

## 5. What Plan 9 was arguing with (found via its own bibliographies)

- T. J. Killian, "Processes as Files", USENIX Summer Conference, Salt
  Lake City, 1984, pp. 203-207.
  The direct ancestor of `/proc`, predating Plan 9.

- Roger Needham, "Names", in *Distributed Systems* (S. Mullender, ed.),
  Addison-Wesley, 1989.
  The naming essay Plan 9 explicitly credits for its namespace design.

- D. M. Ritchie, "A Stream Input-Output System", *AT&T Bell Labs
  Technical Journal* 63(8), 1984.
  Streams, the I/O framework Plan 9 built its network stack on.

- Ousterhout, Cherenson, Douglis, Nelson, Welch, "The Sprite Network
  Operating System", *IEEE Computer* 21(2), 1988, pp. 23-38.
  A contemporary rival "rethink the OS for networks" bet.

- Brent Welch, "A Comparison of Three Distributed File System
  Architectures: Vnode, Sprite, and Plan 9", *Computing Systems* 7(2),
  1994, pp. 175-199.
  The single best source for what the competing bets in this space were.

- B. Clifford Neuman, "The Prospero File System", USENIX File Systems
  Workshop, 1992, pp. 13-28.
  Another naming-based distributed system from the same era.

- John Ousterhout, "Tcl: An Embeddable Command Language", USENIX Winter
  Conference, 1990, pp. 133-146.
  The opposite architectural bet: embed a command interpreter in every
  app for IPC, rather than centralizing routing in one server.

- Vern Paxson, Chris Saltmarsh, "Glish: A User-Level Software Bus for
  Loosely-Coupled Distributed Systems", USENIX Winter Conference, 1993,
  pp. 141-155.
  A message-bus answer to the same "how do independent programs talk"
  problem.

- Steven P. Reiss, "The FIELD Programming Environment", Kluwer, 1995.
  A competing tool-integration architecture using a message-broadcast
  bus instead of files.

- Bob Weiner, "Hyperbole User Manual".
  http://www.cs.indiana.edu/elisp/hyperbole/hyperbole_1.html
  Emacs package implementing context-sensitive "smart buttons" - cited
  by Pike as plumbing's closest analog outside Plan 9, and still alive.

## 6. Descendants: a namespace that runs anywhere

- Dorward, Pike, Presotto, Ritchie, Trickey, Winterbottom, "Inferno",
  IEEE Compcon Proceedings, 1997, pp. 241-244; and "The Inferno
  Operating System", *Bell Labs Technical Journal* 2(1), 1997.
  Plan 9's namespace/9P ideas (Styx, later 9P2000) inside a portable VM
  with the Limbo language.

- Eric Van Hensbergen, Ron Minnich, "Grave Robbers from Outer Space:
  Using 9P2000 Under Linux", USENIX Annual Technical Conference, FREENIX
  track, 2005.
  https://www.usenix.org/legacy/event/usenix05/tech/freenix/full_papers/hensbergen/hensbergen.pdf
  The paper behind Linux's `v9fs`, later reused for `virtio-9p`.

- Russ Cox et al., "Plan 9 from User Space (plan9port)".
  https://github.com/9fans/plan9port
  Ports `rc`, `acme`, `sam`, `mk`, and the plumber to ordinary Unix. The
  actually-actionable way to run Acme and the plumber today.

## 7. Why it stayed niche

- Rob Pike, "Systems Software Research is Irrelevant", slides, 21 Feb
  2000. https://doc.cat-v.org/bell_labs/utah2000/
  (mirror: http://www.herpolhode.com/rob/utah2000.pdf)
  Slides only, no recording. Pike's account of why OS-level research
  stopped mattering once Unix got good enough and hardware got cheap
  enough for inefficiency to stop hurting.

- "Rob Pike Responds", Slashdot interview, 2004.
  https://interviews.slashdot.org/story/04/10/18/1153211/rob-pike-responds
  Plan 9 assumed a network, while Windows and Unix both assumed a
  self-sufficient single box.

- "Unix and Beyond: An Interview with Ken Thompson", *IEEE Computer*
  32(5), 1999. https://cse.unl.edu/~witty/class/csce351/howto/ken_thompson.pdf

- "Plan-9 is definitely NOT a failure", Linux kernel mailing list
  thread, 1999. https://lkml.iu.edu/hypermail/linux/kernel/9905.0/0536.html
  A real-time snapshot of the community arguing about it.

## 8. Lisp machines: output that stays live (Genera & CLIM)

- E. C. Ciccarelli, "Presentation Based User Interfaces", MIT AI Lab
  Technical Report 794, 1984. https://dspace.mit.edu/handle/1721.1/6883
  Almost certainly the origin document, predating Genera 7 by two years
  - the closest primary source to "click on output, get a command."

- "Genera (operating system)", Wikipedia.
  https://en.wikipedia.org/wiki/Genera_(operating_system)
  Click a filename in a listing to view it; click that same on-screen
  filename later to supply it as a command's argument.

- Symbolics, Inc., "Genera User's Guide".
  https://bitsavers.trailing-edge.com/pdf/symbolics/software/genera_8/Genera_User_s_Guide.pdf
  No separate "command levels"; mouse-sensitivity comes free from the
  standard command-definition mechanism.

- "Genera Concepts", retrospective, Symbolics Lisp Machine Museum,
  University of Hamburg.
  https://www.chai.uni-hamburg.de/~moeller/symbolics-info/genera/genera.html

- David Moon & Daniel Weinreb, "The Lisp Machine Manual", MIT AI Lab,
  1981. **(unverified link)**

- Richard Stallman, Daniel Weinreb, David Moon, "LISP Machine Window
  System Manual", LMI, 1983. **(unverified link)**

- David Moon, "The Architecture of the Symbolics 3600", 12th
  International Symposium on Computer Architecture, 1985.
  **(unverified link)**

- David Moon, "Genera Retrospective", IEEE Annals of the History of
  Computing, 1991. **(unverified link)**

- Henry Lieberman, "There's More to Menu Systems than Meets the Screen",
  ACM SIGGRAPH 19(3), 1985, pp. 181-189. **(unverified link)**

- David A. Moon, "Object-Oriented Programming with Flavors", OOPSLA /
  SIGPLAN Notices 21(11), 1986.
  https://www.cs.tufts.edu/~nr/cs257/archive/david-moon/flavors.pdf
  The object system (ancestor of CLOS) that lets every value "know how
  to present itself."

- "CLIM II Specification", International Lisp Associates / Symbolics /
  Xerox / Franz / LispWorks, 1993.
  http://bauhh.dyndns.org:8000/clim-spec/index.html
  Chapter 23 formalizes "Presentation Types" precisely.

- Timothy Moore, "An Implementation of CLIM Presentation Types",
  *Journal of Universal Computer Science* 14(20), 2008.
  https://www.jucs.org/jucs_14_20/an_implementation_of_clim/jucs_14_20_3358_3369_moore.pdf

- R. Rao, W. M. York, D. Doughty, "A Guided Tour of the Common Lisp
  Interface Manager", *Lisp Pointers* 4, 1991. **(unverified link)**

- S. McKay, "CLIM: The Common Lisp Interface Manager", *CACM* 34(9),
  1991, pp. 58-59. **(unverified link)**

- R. Rao, "Implementational Reflection in Silica", ECOOP '91, 1991.
  **(unverified link)**

- "McCLIM", open-source CLIM II implementation, actively maintained.
  https://github.com/McCLIM/McCLIM
  The way to actually use presentation types today.

## 9. Interlisp-D: no context switch at all

- Larry Masinter, "Interlisp: A Sophisticated Interactive Environment
  for Research in AI". https://larrymasinter.net/interlisp-ieee.pdf
  Firsthand retrospective covering DWIM, Masterscope, and the structure
  editor's tight integration.

- "Masterscope Newsletter", Xerox Interlisp-D internal newsletter, Feb
  1985. https://www.bitsavers.org/pdf/xerox/interlisp-d/newsletters/Masterscope_1-01_Feb85.pdf
  A queryable database of program structure that could drive the editor
  to make systematic changes from a single command.

- "Interlisp Reference Manual", The Medley Interlisp Project.
  https://interlisp.org/documentation/IRM.pdf
  Medley is the modern, still-runnable continuation of Interlisp-D.

## 10. Smalltalk-80: the contrasting bet

- Adele Goldberg & David Robson, *Smalltalk-80: The Language and its
  Implementation*, Addison-Wesley, 1983.
  https://archive.org/details/smalltalk80langu00gold
  The "blue book" - canonical language and VM reference.

- Adele Goldberg, *Smalltalk-80: The Interactive Programming
  Environment*, Addison-Wesley, 1984.
  The browsable, inspectable image and its Workspace "do-its" - a
  weaker match to click-to-command than Genera, but the direct ancestor
  of every "live, image-based" environment since.

## 11. Oberon: no command line at all

- Niklaus Wirth & Jurg Gutknecht, "The Oberon System", *Software:
  Practice and Experience* 19(9), 1989, pp. 857-893.
  https://onlinelibrary.wiley.com/doi/abs/10.1002/spe.4380190905
  Any on-screen text matching `Module.Command` executes on a
  middle-click, with previously selected text as its argument. No
  separate command line exists at all.

- Niklaus Wirth & Jurg Gutknecht, *Project Oberon: The Design of an
  Operating System and Compiler*, Addison-Wesley, 1992.
  https://people.inf.ethz.ch/wirth/ProjectOberon1992.pdf
  Free, with source code - the best "go read and run this" entry here.

- Niklaus Wirth, "The Programming Language Oberon", *Software: Practice
  and Experience* 18(7), 1988.
  https://onlinelibrary.wiley.com/doi/10.1002/spe.4380180707

- M. Brandis et al., "The Oberon System Family", *Software: Practice
  and Experience* 25(12), 1995.
  https://onlinelibrary.wiley.com/doi/abs/10.1002/spe.4380251204

- "Using the Mouse and the Keyboard", ETH Oberon tutorial.
  http://www.ethoberon.ethz.ch/ethoberon/tutorial/Mouse.html

## 12. Modern shells and protocols

- Jeffrey Snover, "The Monad Manifesto", 2002.
  https://www.jsnover.com/Docs/MonadManifesto.pdf
  (mirror: https://learn.microsoft.com/en-us/powershell/scripting/developer/monad-manifesto?view=powershell-7.5)
  PowerShell's founding document: Unix's text pipeline is lossy, pass
  structured objects between commands instead.

- "The Monad Manifesto, Annotated", DevOps Collective.
  https://devops-collective-inc.gitbook.io/the-monad-manifesto-annotated

- Fernando Perez & Brian Granger, "IPython: A System for Interactive
  Scientific Computing", *Computing in Science & Engineering* 9(3),
  2007, pp. 21-29. https://bugs.python.org/file28188/ipython07_pe-gr_cise.pdf
  Separating the interactive session (kernel) from the interface as a
  two-process model over sockets - ancestor of Jupyter's kernel/frontend
  split.

- "Jupyter Messaging Protocol", jupyter_client documentation.
  https://jupyter-client.readthedocs.io/en/stable/messaging.html

- "Nushell Philosophy (0.80)", Nushell project.
  https://www.nushell.sh/contributor-book/philosophy_0_80.html
  Keeps Unix's pipe operator but replaces opaque byte streams with typed
  tables and records.

- "Language Server Protocol Specification", Microsoft with Red Hat and
  Codenvy, announced 27 June 2016.
  https://microsoft.github.io/language-server-protocol/
  The editor permanently owns the text buffer and keystrokes; the
  language server only answers structured queries.

- "tmux Control Mode", tmux wiki.
  https://github.com/tmux/tmux/wiki/Control-Mode
  See also iTerm2's integration doc:
  https://iterm2.com/documentation-tmux-integration.html
  `tmux -CC` lets iTerm2 render panes as native GUI windows while tmux
  stays authoritative over the actual shell.

- "The kitty Remote-Control Protocol", kitty terminal documentation.
  https://sw.kovidgoyal.net/kitty/rc_protocol/

## 13. The text-stream case

- "Pipe: How the System Call That Ties Unix Together Came About", The
  New Stack. https://thenewstack.io/pipe-how-the-system-call-that-ties-unix-together-came-about/
  Retells Doug McIlroy's 1964 memo proposing pipes.

- John D. Cook, "Where the Unix Philosophy Breaks Down" and "Unix
  Doesn't Follow the Unix Philosophy".
  https://www.johndcook.com/blog/2010/06/30/where-the-unix-philosophy-breaks-down/
  https://www.johndcook.com/blog/2012/05/25/unix-doesnt-follow-the-unix-philosophy/

- "Plan 9 Papers" (official index), 9p.io.
  https://9p.io/wiki/plan9/Papers/index.html
  The canonical master list - start here to go further.

## Open threads

- Bret Victor's https://worrydream.com/refs is itself a hand-picked
  archive of HCI history papers (hosts the Tesler PDF above); its index
  resisted scraping but is worth browsing directly.
- Thimbleby's original 1982 paper distinguishing modes from contexts is
  cited secondhand everywhere; exact venue unconfirmed.
- Xerox PARC's Mesa/Cedar system and its Tioga editor came up only
  tangentially - a plausible chronological bridge between Interlisp-D
  and Oberon, not covered above.
- No dedicated academic post-mortem titled anything like "why Plan 9
  failed" exists - only Pike's own scattered remarks and the 1999 LKML
  argument, both listed above.
- Several Symbolics-era citations are corroborated by two independent
  bibliographies but their live URLs weren't independently resolved -
  worth checking ACM DL and IEEE Xplore directly.
- No standalone Microsoft blog post announcing LSP, distinct from the
  spec repository itself, turned up.
- No writing from Russ Cox specifically framing "pipes vs. structured
  text" was found, despite his decade on Plan 9 and authorship of
  plan9port - don't cite him for that particular argument.
