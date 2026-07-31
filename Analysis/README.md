# Analysis Archive

Bu klasör, `Makale_Taslagi_I.md` içinde kullanılan analiz girdilerini, kodları,
not defterlerini, ara çıktıları, nihai tabloları ve şekilleri tek yerde toplar.
Özgün klasörler silinmemiş veya taşınmamış; makaledeki göreli bağlantıların
bozulmaması için buraya kopyalanmıştır.

## Klasör yapısı

- `00_Methodology_and_Inputs`: yöntem, sektör eşlemesi ve metodoloji şemaları.
- `01_SNII`: ağ göstergeleri, SNII kodları, ara çıktılar, tablolar ve şekiller.
- `02_CGE_EIPI`: SAM/CGE çözümleri, karşı-olgusal şoklar ve EIPI sonuçları.
- `03_Hypothesis_Tests`: birleşik panel, H1-H4 testleri, sınıflama ve sağlamlık çıktıları.
- `04_Robust_Core_and_Policy`: Pareto katmanları, dengeli skor, 96 spesifikasyon ve politika senaryoları.
- `05_Elasticity_Sensitivity`: Ek C'deki CET-Armington ve CES duyarlılık analizleri.

## Makaledeki ana kanıt zinciri

1. `01_SNII`: Şekil 2-4 ve ana SNII tabloları.
2. `02_CGE_EIPI`: Şekil 5-6 ve CGE/EIPI ana sonuçları.
3. `03_Hypothesis_Tests`: H1-H4 sonuçları ve Şekil 7.
4. `04_Robust_Core_and_Policy`: Şekil 8-9, Pareto, sağlam çekirdek ve politika sonuçları.
5. `05_Elasticity_Sensitivity`: Ek C, Tablo C1-C2 sonuçları ve yeniden üretim betiği.

## Duyarlılık analizini yeniden üretme

Çalışma dizininden:

```powershell
python "Analysis/05_Elasticity_Sensitivity/CGE_Sensitivity_Analysis.py"
```

Betik, sonuçları aynı klasörde CSV ve Excel olarak üretir. Ek C'nin yeniden
üretimi için gereken çalışabilir model kodu ve SAM girdisi duyarlılık paketiyle
birlikte aynı klasörde tutulur.

## Bütünlük denetimi

- Makalede kullanılan dört ana analiz aşamasındaki dosyalar eksiksiz kopyalanmıştır.
- Klasör adlarında geliştirme aracı veya model adı kullanılmamıştır.
- Makalede bağlantı verilen dokuz ana görselin arşivde bulunduğu doğrulanmıştır.
- Duyarlılık referansı, yeniden üretim betiği, CSV ve üç sayfalı Excel sonuç dosyası eklenmiştir.
- Güncel arşiv toplamı 83 dosya ve yaklaşık 15.79 MB'dir.
