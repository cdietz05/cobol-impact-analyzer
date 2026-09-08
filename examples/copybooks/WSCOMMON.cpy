      *****************************************************************
      * WSCOMMON  - SHARED WORK FIELDS AND THE CUSTOMER REPORT LINE   *
      *****************************************************************
       01  WS-REPORT-LINE.
           05  RL-CUST-ID           PIC 9(9).
           05  FILLER               PIC X(02)  VALUE SPACES.
           05  RL-CUST-NAME         PIC X(30).
           05  FILLER               PIC X(02)  VALUE SPACES.
           05  RL-CITY-STATE        PIC X(24).
           05  FILLER               PIC X(02)  VALUE SPACES.
           05  RL-BALANCE           PIC ZZ,ZZZ,ZZ9.99-.
      *
      * REDEFINES THE WHOLE PRINT LINE FOR THE SPOOL WRITER
      *
       01  WS-REPORT-FLAT  REDEFINES  WS-REPORT-LINE  PIC X(83).
      *
       01  WS-NAME-KEY              PIC X(30).
       01  WS-SEARCH-NAME           PIC X(30).
       01  WS-FULL-ADDRESS          PIC X(70).
