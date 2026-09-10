      *****************************************************************
      * CSACCT   - HOST VARIABLE RECORD FOR TABLE CSS_ACCOUNT         *
      *            ONE ROW PER BILLABLE ACCOUNT UNDER A CUSTOMER      *
      *****************************************************************
       01  CS-ACCOUNT-REC.
           05  CS-ACCT-NBR          PIC S9(9)       COMP-3.
           05  CS-ACCT-CUST-ID      PIC S9(9)       COMP-3.
           05  CS-ACCT-TYPE         PIC X(04).
           05  CS-ACCT-OPEN-DT      PIC X(10).
           05  CS-ACCT-BILL-NAME    PIC X(30).
           05  CS-ACCT-BALANCE      PIC S9(11)V99   COMP-3.
           05  CS-ACCT-STATUS       PIC X(02).
