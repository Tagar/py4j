# py4j perf report

**Branch:** perf-framework (rev 8f3b264, dirty)  
**Timestamp:** 2026-05-07T03:53:27+00:00  
**OS / CPU:** Darwin 25.4.0 (arm64) - Apple M4 Pro @ 0.00 GHz, 14 physical / 14 logical cores  
**RAM:** 48.0 GB  
**Python / Java:** 3.14.3 (CPython) / openjdk version "21.0.10" 2026-01-20 LTS  
**py4j:** 0.10.9.9
**Process priority:** nice=-15 (renice not applied: --no-renice)

| ID | Scenario | Median | p95 | Stddev | Noise | Rounds |
|----|----------|--------|-----|--------|-------|--------|
| M1 | m1_static_call_no_args | 22.498 µs | 28.412 µs | 2.191 µs | 28.0% | 50 |
| M2a | m2a_instance_append_int | 44.379 µs | 48.999 µs | 2.281 µs | 12.8% | 50 |
| M2b | m2b_instance_append_str | 45.944 µs | 49.092 µs | 1.737 µs | 11.1% | 50 |
| M3 | m3_jvmview_class_resolution | 83.671 µs | 90.270 µs | 2.261 µs | 8.8% | 50 |
| M4 | m4_constructor_and_finalize | 140.921 µs | 149.672 µs | 4.461 µs | 9.1% | 50 |
| M5a | m5a_encode_int | 215.831 ns | 226.659 ns | 25.160 ns | 15.2% | 507407 |
| M5b | m5b_encode_string | 368.616 ns | 387.851 ns | 38.752 ns | 15.7% | 1875123 |
| M5c | m5c_encode_float | 343.745 ns | 354.121 ns | 31.481 ns | 13.6% | 1919753 |
| M6a | m6a_decode_int | 135.000 ns | 139.590 ns | 9.730 ns | 13.9% | 672271 |
| M6b | m6b_decode_string | 429.465 ns | 448.692 ns | 55.212 ns | 14.9% | 1935368 |
| M7a | m7a_escape | 829.203 ns | 933.302 ns | 72.398 ns | 14.1% | 1126771 |
| M7b | m7b_unescape | 3.333 µs | 3.479 µs | 348.683 ns | 15.0% | 1610805 |
| X1-1 | concurrent_1_thread | 223.507 ms | 224.769 ms | 1.168 ms | 1.5% | 10 |
| X1-4 | concurrent_4_threads | 129.958 ms | 130.760 ms | 662.068 µs | 1.4% | 10 |
| X1-16 | concurrent_16_threads | 145.999 ms | 147.328 ms | 1.297 ms | 2.3% | 10 |
| X2-1k | iterate_javalist_1k | 213.685 ms | 218.200 ms | 2.841 ms | 3.4% | 10 |
| X2-10k | iterate_javalist_10k | 216.480 ms | 224.144 ms | 4.108 ms | 5.4% | 10 |
| X2-100k | iterate_javalist_100k | 2.078 s | 2.100 s | 13.409 ms | 1.9% | 10 |
| X3-100 | listconverter_100 | 48.247 ms | 50.088 ms | 875.796 µs | 4.9% | 10 |
| X3-1k | listconverter_1k | 232.407 ms | 234.470 ms | 2.333 ms | 2.8% | 10 |
| X3-10k | listconverter_10k | 234.870 ms | 235.312 ms | 555.499 µs | 0.7% | 10 |
| X4 | callback_sort_100_items | 95.150 ms | 99.113 ms | 1.657 ms | 4.6% | 10 |
| X5 | error_path_latency | 51.148 ms | 56.071 ms | 2.818 ms | 14.6% | 10 |
| X6 | pool_saturation_50_threads | 83.589 ms | 89.845 ms | 3.404 ms | 10.3% | 10 |

*Noise = (p95 - p5) / median within a single run.*
