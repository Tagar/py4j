# py4j perf report

**Branch:** perf-framework (rev ced9ef6)  
**Timestamp:** 2026-04-19T21:48:03+00:00  
**OS / CPU:** Darwin 25.4.0 (arm64) - Apple M4 Pro @ 0.00 GHz, 14 physical / 14 logical cores  
**RAM:** 48.0 GB  
**Python / Java:** 3.14.3 (CPython) / openjdk version "21.0.10" 2026-01-20 LTS  
**py4j:** 0.10.9.9
**Process priority:** nice=0 (renice not applied: renice-exit-1)

| ID | Scenario | Median | p95 | Stddev | Noise | Rounds |
|----|----------|--------|-----|--------|-------|--------|
| M1 | m1_static_call_no_args | 21.416 µs | 27.500 µs | 5.970 µs | 43.8% | 101911 |
| M2a | m2a_instance_append_int | 42.625 µs | 53.167 µs | 12.444 µs | 38.2% | 38667 |
| M2b | m2b_instance_append_str | 42.709 µs | 53.625 µs | 12.285 µs | 39.4% | 39709 |
| M3 | m3_jvmview_class_resolution | 83.833 µs | 98.990 µs | 17.025 µs | 26.9% | 21946 |
| M4 | m4_constructor_and_finalize | 126.792 µs | 157.811 µs | 27.760 µs | 31.9% | 15112 |
| M5a | m5a_encode_int | 204.170 ns | 235.000 ns | 20.868 ns | 21.8% | 460660 |
| M5b | m5b_encode_string | 358.923 ns | 394.231 ns | 57.247 ns | 17.9% | 1860467 |
| M5c | m5c_encode_float | 343.750 ns | 364.563 ns | 50.261 ns | 16.7% | 2000000 |
| M6a | m6a_decode_int | 134.590 ns | 145.000 ns | 13.911 ns | 18.0% | 792080 |
| M6b | m6b_decode_string | 435.923 ns | 458.308 ns | 60.784 ns | 15.4% | 1904764 |
| M7a | m7a_escape | 825.000 ns | 941.600 ns | 108.671 ns | 16.2% | 1218324 |
| M7b | m7b_unescape | 3.312 µs | 3.480 µs | 431.574 ns | 15.7% | 1621535 |
| X1-1 | concurrent_1_thread | 213.431 ms | 214.540 ms | 859.007 µs | 1.2% | 10 |
| X1-4 | concurrent_4_threads | 127.727 ms | 129.360 ms | 850.145 µs | 1.9% | 10 |
| X1-16 | concurrent_16_threads | 142.938 ms | 146.031 ms | 2.200 ms | 4.6% | 10 |
| X2-1k | iterate_javalist_1k | 39.729 ms | 40.758 ms | 7.272 ms | 42.3% | 10 |
| X2-10k | iterate_javalist_10k | 199.144 ms | 203.204 ms | 2.283 ms | 3.4% | 10 |
| X2-100k | iterate_javalist_100k | 1.918 s | 1.933 s | 6.881 ms | 1.0% | 10 |
| X3-100 | listconverter_100 | 7.435 ms | 7.804 ms | 806.066 µs | 26.3% | 10 |
| X3-1k | listconverter_1k | 39.571 ms | 42.111 ms | 7.011 ms | 42.5% | 10 |
| X3-10k | listconverter_10k | 215.823 ms | 221.527 ms | 3.313 ms | 4.3% | 10 |
| X4 | callback_sort_100_items | 21.825 ms | 22.223 ms | 3.496 ms | 41.5% | 10 |
| X5 | error_path_latency | 51.260 ms | 55.922 ms | 3.163 ms | 15.8% | 10 |
| X6 | pool_saturation_50_threads | 19.082 ms | 20.048 ms | 838.401 µs | 12.4% | 10 |

*Noise = (p95 - p5) / median within a single run.*
