      *****************************************************************
      * ORDERREC  - HOST VARIABLE RECORD FOR TABLES ORDERS/ORDER_SHIP *
      *****************************************************************
       01  ORDER-REC.
           05  ORD-NBR              PIC S9(9)       COMP-3.
           05  ORD-CUST-ID          PIC S9(9)       COMP-3.
           05  ORD-SHIP-NAME        PIC X(30).
           05  ORD-TOTAL            PIC S9(7)V99    COMP-3.
           05  ORD-STATUS           PIC X(02).
