      *****************************************************************
      * CSWORK   - SHARED WORK FIELDS AND THE CUSTOMER REPORT LINE    *
      *****************************************************************
       01  CS-REPORT-LINE.
           05  CS-RL-CUST-ID        PIC 9(9).
           05  FILLER               PIC X(02)  VALUE SPACES.
           05  CS-RL-NAME           PIC X(30).
           05  FILLER               PIC X(02)  VALUE SPACES.
           05  CS-RL-CITY-STATE     PIC X(24).
           05  FILLER               PIC X(02)  VALUE SPACES.
           05  CS-RL-BALANCE        PIC ZZ,ZZZ,ZZ9.99-.
      *
      * FLAT REDEFINE FOR THE SPOOL WRITER
      *
       01  CS-RL-FLAT  REDEFINES  CS-REPORT-LINE  PIC X(83).
      *
       01  CS-SEARCH-NAME           PIC X(30).
       01  CS-CUST-KEY              PIC X(40).
       01  CS-MSG-TEXT             PIC X(80).
       01  CS-SHORT-NAME            PIC X(20).
