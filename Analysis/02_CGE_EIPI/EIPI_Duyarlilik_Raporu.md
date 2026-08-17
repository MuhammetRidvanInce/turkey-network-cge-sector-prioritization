# EIPI Duyarlılık Raporu — Heterojen Sektörel Esneklikler

Karşılaştırmaların tamamında birincil ölçüt `EIPI_CH_size_controlled` kullanılmıştır.
Ana modelde esneklikler Agriculture, Industry ve Service gruplarına göre sektör bazında atanmıştır.

| Scenario | Description | Successful solutions | Baseline termination | Spearman rho | p-value | Top-10 overlap | Bottom-10 overlap | Quartile changes | Runtime seconds |
|---|---|---|---|---|---|---|---|---|---|
| shock_5pct | TFP shock: 5% | 62/62 | optimal | 0.9997 | 0.0000 | 10/10 | 10/10 | 0 | 24.7204 |
| shock_20pct | TFP shock: 20% | 62/62 | optimal | 0.9992 | 0.0000 | 10/10 | 10/10 | 4 | 26.2928 |
| size_normalized | Output-size-normalized TFP shock | 62/62 | optimal | 0.7159 | 0.0000 | 5/10 | 7/10 | 38 | 26.1171 |
| omega_plus25 | Sector-specific omega +25% | 62/62 | optimal | 0.9996 | 0.0000 | 10/10 | 9/10 | 0 | 26.4112 |
| omega_minus25 | Sector-specific omega -25% | 62/62 | optimal | 0.9996 | 0.0000 | 10/10 | 10/10 | 2 | 26.8997 |
| psi_plus25 | Sector-specific psi +25% | 62/62 | optimal | 0.9998 | 0.0000 | 10/10 | 9/10 | 0 | 26.5822 |
| psi_minus25 | Sector-specific psi -25% | 62/62 | optimal | 0.9994 | 0.0000 | 10/10 | 10/10 | 2 | 26.3359 |
| sigma_plus25 | Sector-specific sigma +25% | 62/62 | optimal | 0.9993 | 0.0000 | 10/10 | 10/10 | 2 | 26.7893 |
| sigma_minus25 | Sector-specific sigma -25% | 62/62 | optimal | 0.9994 | 0.0000 | 10/10 | 10/10 | 4 | 27.0043 |
| fixed_wage_closure | Fixed-wage numeraire | 62/62 | optimal | 0.9482 | 0.0000 | 8/10 | 8/10 | 12 | 25.8199 |

`sigma_plus25` senaryosunda hizmet sektörleri için sigma tam olarak 1'e ulaştığından,
Armington bileşimi CES'in Cobb–Douglas sınırı kullanılarak çözülmüştür.