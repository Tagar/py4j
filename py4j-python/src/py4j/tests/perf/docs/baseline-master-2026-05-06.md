# py4j perf report

**Branch:** perf-framework (rev b940a62, dirty)  
**Timestamp:** 2026-05-07T03:21:56+00:00  
**OS / CPU:** Darwin 25.4.0 (arm64) - Apple M4 Pro @ 0.00 GHz, 14 physical / 14 logical cores  
**RAM:** 48.0 GB  
**Python / Java:** 3.14.3 (CPython) / openjdk version "21.0.10" 2026-01-20 LTS  
**py4j:** 0.10.9.9
**Process priority:** nice=-15 (reniced from -15)

| ID | Scenario | Median | p95 | Stddev | Noise | Rounds |
|----|----------|--------|-----|--------|-------|--------|
| M1 | m1_static_call_no_args | 22.667 µs | 28.875 µs | 6.989 µs | 39.5% | 72008 |
| M2a | m2a_instance_append_int | 44.584 µs | 52.875 µs | 11.647 µs | 28.4% | 39057 |
| M2b | m2b_instance_append_str | 44.792 µs | 54.750 µs | 12.853 µs | 32.7% | 35027 |
| M3 | m3_jvmview_class_resolution | 84.000 µs | 99.502 µs | 17.392 µs | 26.3% | 20600 |
| M4 | m4_constructor_and_finalize | 135.855 µs | 168.164 µs | 30.140 µs | 34.7% | 14822 |
| M5a | m5a_encode_int | 214.170 ns | 272.910 ns | 22.989 ns | 37.9% | 498951 |
| M5b | m5b_encode_string | 371.997 ns | 392.860 ns | 33.999 ns | 16.0% | 1966966 |
| M5c | m5c_encode_float | 343.753 ns | 364.569 ns | 31.214 ns | 16.7% | 1951238 |
| M6a | m6a_decode_int | 134.590 ns | 138.340 ns | 8.952 ns | 13.3% | 771665 |
| M6b | m6b_decode_string | 424.175 ns | 443.183 ns | 41.120 ns | 15.2% | 1610563 |
| M7a | m7a_escape | 816.700 ns | 912.510 ns | 65.367 ns | 13.3% | 1165101 |
| M7b | m7b_unescape | 3.250 µs | 3.395 µs | 265.176 ns | 15.4% | 1643944 |
| X1-1 | concurrent_1_thread | 222.911 ms | 224.436 ms | 1.360 ms | 1.7% | 10 |
| X1-4 | concurrent_4_threads | 130.952 ms | 131.699 ms | 643.313 µs | 1.4% | 10 |
| X1-16 | concurrent_16_threads | 144.163 ms | 146.545 ms | 1.805 ms | 3.6% | 10 |
| X2-1k | iterate_javalist_1k | 38.641 ms | 42.256 ms | 7.591 ms | 46.0% | 10 |
| X2-10k | iterate_javalist_10k | 215.968 ms | 219.655 ms | 2.414 ms | 3.2% | 10 |
| X2-100k | iterate_javalist_100k | 2.116 s | 2.130 s | 14.086 ms | 1.9% | 10 |
| X3-100 | listconverter_100 | 8.215 ms | 8.543 ms | 899.395 µs | 26.5% | 10 |
| X3-1k | listconverter_1k | 39.771 ms | 42.208 ms | 6.410 ms | 39.9% | 10 |
| X3-10k | listconverter_10k | 225.721 ms | 229.500 ms | 2.625 ms | 3.3% | 10 |
| X4 | callback_sort_100_items | 21.799 ms | 23.263 ms | 3.763 ms | 47.1% | 10 |
| X5 | error_path_latency | 51.116 ms | 57.317 ms | 3.591 ms | 17.4% | 10 |
| X6 | pool_saturation_50_threads | 20.265 ms | 21.156 ms | 1.096 ms | 15.5% | 10 |

*Noise = (p95 - p5) / median within a single run.*
