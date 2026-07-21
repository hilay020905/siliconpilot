`timescale 1ns/1ps

module tb_accumulator;
    reg        clk = 0;
    reg        rst = 1;      // realistic integration: reset is asserted at power-up,
                              // held for a couple cycles, then released - exactly what
                              // any real SoC integration does. accumulator.v's seeded
                              // bug is that it ignores this signal entirely.
    reg        en  = 0;
    reg  [7:0] data_in = 0;
    wire [7:0] acc_out;

    accumulator dut (
        .clk(clk), .rst(rst), .en(en), .data_in(data_in), .acc_out(acc_out)
    );

    always #5 clk = ~clk;

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_accumulator);

        // Standard power-up sequence: hold reset for 2 clock edges, then release
        // on a negedge (mid-cycle) - never change a signal exactly on the same
        // edge the DUT samples it on, which is a classic testbench race hazard.
        @(posedge clk);
        @(posedge clk);
        @(negedge clk);
        rst = 0;

        // Check 1: immediately after reset release, before any legitimate write,
        // acc_out should read a known value (0), not X.
        #1;
        if (acc_out === 8'h00) begin
            $display("CHECK PASS initial_value_known");
        end else begin
            $display("CHECK FAIL: acc_out is %b at t=1ns, expected 8'h00 (X-propagation from unreset register)", acc_out);
        end

        // Check 2: after a real write, the value should be exactly data_in (since
        // start value should have been 0). Drive stimulus mid-cycle (negedge),
        // let it settle well before the posedge that must capture it.
        data_in = 8'h05;
        en = 1;
        @(posedge clk);       // this edge captures en=1/data_in=5
        @(negedge clk);       // safely past the edge before changing en again
        en = 0;
        #1;
        if (acc_out === 8'h05) begin
            $display("CHECK PASS accumulate_from_known_zero");
        end else begin
            $display("CHECK FAIL: acc_out is %b after one write, expected 8'h05", acc_out);
        end

        #10;
        $finish;
    end
endmodule
