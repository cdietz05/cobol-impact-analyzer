      *****************************************************************
      * CSCUST   - HOST VARIABLE RECORD FOR TABLE CSS_CUSTOMER        *
      *            CUSTOMER SERVICE SYSTEM - CUSTOMER MASTER          *
      *****************************************************************
       01  CS-CUSTOMER-REC.
           05  CS-CUST-ID           PIC S9(9)       COMP-3.
           05  CS-CUST-NAME         PIC X(30).
           05  CS-CUST-ADDR-1       PIC X(30).
           05  CS-CUST-ADDR-2       PIC X(30).
           05  CS-CUST-CITY         PIC X(20).
           05  CS-CUST-STATE        PIC X(02).
           05  CS-CUST-ZIP          PIC X(10).
           05  CS-CUST-PHONE        PIC X(10).
           05  CS-CUST-BALANCE      PIC S9(9)V99    COMP-3.
           05  CS-CUST-CYCLE        PIC 9(02).
           05  CS-CUST-STATUS       PIC X(01).
      *
      * NULL INDICATORS - ONE PER NULLABLE COLUMN
      *
       01  CS-CUSTOMER-IND.
           05  IND-CUST-NAME        PIC S9(4)       COMP.
           05  IND-CUST-BALANCE     PIC S9(4)       COMP.
