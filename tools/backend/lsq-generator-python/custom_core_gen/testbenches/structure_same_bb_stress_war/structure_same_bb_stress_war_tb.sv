// structure_same_bb_stress_war: see ../same_bb_stress_body.sv and structure-config.json.
`define TB_MODULE  structure_same_bb_stress_war_tb
`define DUT_MODULE structure_same_bb_stress_war
`define WAR_ONLY
`include "../same_bb_stress_body.sv"
