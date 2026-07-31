# EIPI Duyarlılık Raporu — Heterojen Sektörel Esneklikler

Karşılaştırmaların tamamında birincil ölçüt `EIPI_CH_size_controlled` kullanılmıştır.
Ana modelde esneklikler Agriculture, Industry ve Service gruplarına göre sektör bazında atanmıştır.

| Scenario | Description | Successful solutions | Baseline termination | Spearman rho | p-value | Top-10 overlap | Bottom-10 overlap | Quartile changes | Runtime seconds |
|---|---|---|---|---|---|---|---|---|---|
| shock_5pct | TFP shock: 5% | 62/62 | optimal | 0.9998 | 0.0000 | 10/10 | 10/10 | 0 | 23.2492 |
| shock_20pct | TFP shock: 20% | 62/62 | optimal | 0.9994 | 0.0000 | 10/10 | 9/10 | 2 | 23.8875 |
| size_normalized | Output-size-normalized TFP shock | 62/62 | optimal | 0.7143 | 0.0000 | 5/10 | 6/10 | 39 | 24.9865 |
| omega_plus25 | Sector-specific omega +25% | 62/62 | optimal | 0.9998 | 0.0000 | 10/10 | 9/10 | 0 | 24.7116 |
| omega_minus25 | Sector-specific omega -25% | 62/62 | optimal | 0.9998 | 0.0000 | 10/10 | 9/10 | 0 | 25.5312 |
| psi_plus25 | Sector-specific psi +25% | 62/62 | optimal | 0.9998 | 0.0000 | 10/10 | 9/10 | 0 | 24.7878 |
| psi_minus25 | Sector-specific psi -25% | 62/62 | optimal | 0.9997 | 0.0000 | 10/10 | 10/10 | 0 | 24.7884 |
| sigma_plus25 | Sector-specific sigma +25% | 62/62 | optimal | 0.9998 | 0.0000 | 10/10 | 10/10 | 0 | 25.0656 |
| sigma_minus25 | Sector-specific sigma -25% | 62/62 | optimal | 0.9995 | 0.0000 | 10/10 | 9/10 | 0 | 25.0573 |
| fixed_wage_closure | Fixed-wage numeraire | 62/62 | optimal | 0.9440 | 0.0000 | 8/10 | 7/10 | 12 | 23.7716 |

`sigma_plus25` senaryosunda hizmet sektörleri için sigma tam olarak 1'e ulaştığından,
Armington bileşimi CES'in Cobb–Douglas sınırı kullanılarak çözülmüştür.