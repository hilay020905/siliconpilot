module accumulator (
    input        clk,
    input        rst,
    input        en,
    input  [7:0] data_in,
    output [7:0] acc_out
);

    reg [7:0] acc_reg;

    // BUG (seeded): this always block never checks rst, so acc_reg
    // powers up as X and is never initialized to a known value.
    always @(posedge clk) begin
        if (en) begin
            acc_reg <= acc_reg + data_in;
        end
    end

    assign acc_out = acc_reg;

endmodule
