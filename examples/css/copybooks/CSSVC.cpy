      *****************************************************************
      * CSSVC    - HOST VARIABLE RECORD FOR TABLE CSS_SVC_ORDER       *
      *            FIELD SERVICE / INSTALL / REPAIR WORK ORDERS       *
      *****************************************************************
       01  CS-SVC-ORDER-REC.
           05  CS-SVC-ORDER-NBR     PIC S9(10)      COMP-3.
           05  CS-SVC-ACCT-NBR      PIC S9(9)       COMP-3.
           05  CS-SVC-CUST-NAME     PIC X(30).
           05  CS-SVC-TYPE          PIC X(06).
           05  CS-SVC-STATUS        PIC X(02).
           05  CS-SVC-TECH-NOTES    PIC X(60).
