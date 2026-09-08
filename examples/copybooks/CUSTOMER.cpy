      *****************************************************************
      * CUSTOMER  - HOST VARIABLE RECORD FOR TABLE CUSTOMER           *
      *****************************************************************
       01  CUSTOMER-REC.
           05  CUST-ID              PIC S9(9)       COMP-3.
           05  CUST-NAME            PIC X(30).
           05  CUST-ADDR-1          PIC X(40).
           05  CUST-CITY            PIC X(20).
           05  CUST-STATE           PIC X(02).
           05  CUST-ZIP             PIC X(10).
           05  CUST-BALANCE         PIC S9(9)V99    COMP-3.
           05  CUST-STATUS          PIC X(01).
      *
      * NULL INDICATORS - ONE PER NULLABLE COLUMN ABOVE
      *
       01  CUSTOMER-IND.
           05  IND-CUST-NAME        PIC S9(4)       COMP.
           05  IND-CUST-ADDR-1      PIC S9(4)       COMP.
           05  IND-CUST-BALANCE     PIC S9(4)       COMP.
