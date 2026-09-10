      *****************************************************************
      * CSXTRACT - FLAT EXTRACT RECORD WRITTEN BY THE BILLING CYCLE.  *
      *            LOADED DOWNSTREAM INTO THE REVENUE WAREHOUSE.      *
      *****************************************************************
       01  CS-XTRACT-REC.
           05  XT-CUST-ID           PIC 9(09).
           05  XT-CUST-NAME         PIC X(30).
           05  XT-ACCT-NBR          PIC 9(09).
           05  XT-BALANCE           PIC S9(11)V99   COMP-3.
           05  XT-CYCLE             PIC 9(02).
           05  XT-FILLER            PIC X(30).
