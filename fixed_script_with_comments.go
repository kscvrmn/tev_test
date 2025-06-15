package main

import (
	"context"  // нет смысов назвать context2, context удобнее
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"  // добавлен более удобный пакет логирования ошибок вместо простых fmt.Printf
	"math"
	"net/http"
	"os"
	"runtime"  // добавлен для определения оптимального количества горутин
	"sync"
	"time"
)

func MinInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func MaxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func MaxFloat32(a, b float32) float32 {
	if a > b {
		return a
	}
	return b
}

type Point struct {
	X int `json:"x"`
	Y int `json:"y"`
}

type WeightedPoint struct {
	Point
	Weight float32 `json:"weight"`
}

type Polygon struct {
	Points []WeightedPoint `json:"points"`
}

"""поля переименованы с большой буквы для экспорта в json"""
"""изначально x1, y1, x2, y2 не экспортировались, что приводило к пустым значениям в jsone"""
type Bbox struct {
	X1 int `json:"x1"`
	Y1 int `json:"y1"`
	X2 int `json:"x2"`
	Y2 int `json:"y2"`
}

type Result struct {
	Bbox          Bbox       `json:"bbox"`
	MaxWeight     float32    `json:"max_weight"`
	HeavyPolygons []*Polygon `json:"heavy_polygons"`
}

// Добавлен новый тип для результатов обработки отдельных полигонов
// Это предотвращает гонки данных, так как каждый воркер работает с локальной копией
type PolygonResult struct {
	localBbox Bbox      // Локальный bbox для безопасной конкурентной обработки
	weight    float32   // Вес полигона
	isHeavy   bool      // Флаг "тяжелого" полигона для оптимизации добавления в результат
	polygon   *Polygon  // Указатель на сам полигон для экономии памяти
	err       error     // Ошибка для корректной обработки сбоев
}

// Добавлены новые параметры командной строки для большей гибкости:
// - serverURL позволяет указать адрес сервера вместо жестко закодированного
// - numWorkers позволяет контролировать параллелизм вместо фиксированных 10 горутин
var (
	timeout     = flag.Int("timeout", 60, "максимальное время обработки в секундах")
	polygonsNum = flag.Int("polygons_num", 3, "количество многоугольников для обработки")
	serverURL   = flag.String("url", "http://localhost:8080/polygon", "URL для получения многоугольников")
	numWorkers  = flag.Int("workers", runtime.NumCPU(), "количество рабочих горутин")
)

func main() {
	flag.Parse()

	// Исправлено: используем стандартный импорт context вместо context2
	// Контекст с таймаутом для правильного прерывания всех операций
	ctx, cancel := context.WithTimeout(context.Background(), time.Second*time.Duration(*timeout))
	defer cancel()

	// Реорганизация архитектуры для устранения гонок данных:
	// - используем каналы для координации работы
	// - разделяем загрузку, обработку и агрегацию результатов
	indices := make(chan int, *polygonsNum)
	results := make(chan PolygonResult, 100)
	var wg sync.WaitGroup

	// Запускаем воркеров динамически, основываясь на доступных CPU или параметре командной строки
	// Это более эффективно, чем фиксированные 10 горутин из исходного кода
	for i := 0; i < *numWorkers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-ctx.Done():
					// Обработка таймаута: завершаем воркера при истечении времени
					return
				case idx, ok := <-indices:
					if !ok {
						// Корректное завершение при закрытии канала индексов
						return
					}
					// Вынесено в отдельную функцию для лучшей модульности и тестируемости
					polygonResult := fetchAndProcessPolygon(ctx, idx)
					
					// Правильная обработка отправки результата с учетом возможного таймаута
					select {
					case <-ctx.Done():
						return
					case results <- polygonResult:
					}
				}
			}
		}()
	}

	// Отдельная горутина для подачи индексов в канал
	// Это предотвращает блокировку основного потока
	go func() {
		for i := 0; i < *polygonsNum; i++ {
			select {
			case <-ctx.Done():
				close(indices)
				return
			case indices <- i:
			}
		}
		close(indices)
	}()

	// Отдельный канал для финального результата
	// Позволяет корректно обрабатывать ситуацию с таймаутом
	resCh := make(chan Result)
	go collectResults(ctx, results, *polygonsNum, resCh)

	// Отдельная горутина для ожидания завершения всех воркеров
	// Это позволяет корректно закрыть канал results после завершения всех обработчиков
	go func() {
		wg.Wait()
		close(results)
	}()

	// Ожидание результата или таймаута с корректной обработкой ошибок
	select {
	case <-ctx.Done():
		log.Printf("Превышено время выполнения (%d сек)", *timeout)
		os.Exit(1)
	case result := <-resCh:
		// Исправлено форматирование вывода JSON с отступами для лучшей читаемости
		output, err := json.MarshalIndent(result, "", "  ")
		if err != nil {
			log.Fatalf("Ошибка сериализации JSON: %v", err)
		}
		fmt.Println(string(output))
	}
}

// Разделение монолитной функции для улучшения тестируемости и модульности
// Загрузка и обработка полигона теперь в отдельной функции
func fetchAndProcessPolygon(ctx context.Context, idx int) PolygonResult {
	// Создаем HTTP-клиент с явным таймаутом вместо использования DefaultClient
	// Это предотвращает зависание запросов
	client := &http.Client{
		Timeout: 30 * time.Second,
	}
	
	// Используем запрос с контекстом для поддержки отмены по таймауту
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, *serverURL, nil)
	if err != nil {
		return PolygonResult{err: fmt.Errorf("ошибка создания запроса: %v", err)}
	}
	
	// Детальная обработка ошибок HTTP вместо простого "fail"
	resp, err := client.Do(req)
	if err != nil {
		return PolygonResult{err: fmt.Errorf("ошибка HTTP запроса: %v", err)}
	}
	defer resp.Body.Close() // Добавлен для предотвращения утечек ресурсов
	
	if resp.StatusCode != http.StatusOK {
		return PolygonResult{err: fmt.Errorf("некорректный статус ответа: %d", resp.StatusCode)}
	}
	
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return PolygonResult{err: fmt.Errorf("ошибка чтения ответа: %v", err)}
	}
	
	var poly Polygon
	err = json.Unmarshal(respBody, &poly)
	if err != nil {
		return PolygonResult{err: fmt.Errorf("ошибка разбора JSON: %v", err)}
	}
	
	// Вынесено в отдельную функцию для разделения загрузки и обработки
	return processPolygon(&poly, ctx)
}

// Вынесено в отдельную функцию для улучшения модульности и тестируемости
func processPolygon(poly *Polygon, ctx context.Context) PolygonResult {
	// Обработка краевого случая с пустым полигоном
	if len(poly.Points) == 0 {
		return PolygonResult{
			localBbox: Bbox{},
			weight:    0,
			isHeavy:   false,
			polygon:   poly,
		}
	}

	// Инициализация bbox первой точкой для корректных начальных значений
	bbox := Bbox{
		X1: poly.Points[0].X,
		Y1: poly.Points[0].Y,
		X2: poly.Points[0].X,
		Y2: poly.Points[0].Y,
	}

	// Вычисление суммарного веса и обновление bbox
	sumWeight := float32(0)
	for i, p := range poly.Points {
		// Регулярная проверка контекста для раннего прерывания при таймауте
		// Это важно для больших полигонов (до 1М точек)
		if i%1000 == 0 && ctx.Err() != nil {
			return PolygonResult{err: ctx.Err()}
		}
		
		sumWeight += p.Weight
		bbox.X1 = MinInt(bbox.X1, p.X)
		bbox.Y1 = MinInt(bbox.Y1, p.Y)
		bbox.X2 = MaxInt(bbox.X2, p.X)
		bbox.Y2 = MaxInt(bbox.Y2, p.Y)
	}

	// Критерий "тяжелого" полигона согласно ТЗ (>= 100)
	isHeavy := sumWeight >= 100

	// Возвращаем структурированный результат для последующей агрегации
	return PolygonResult{
		localBbox: bbox,
		weight:    sumWeight,
		isHeavy:   isHeavy,
		polygon:   poly,
	}
}

// Отдельная функция для безопасной агрегации результатов 
// Устраняет гонки данных, так как только один поток модифицирует Result
func collectResults(ctx context.Context, results chan PolygonResult, total int, resCh chan Result) {
	// Инициализация начальных значений bbox для корректного поиска минимума/максимума
	result := Result{
		Bbox: Bbox{
			X1: math.MaxInt,
			Y1: math.MaxInt,
			X2: math.MinInt,
			Y2: math.MinInt,
		},
		MaxWeight:     0,
		HeavyPolygons: []*Polygon{},
	}

	// Отслеживаем количество обработанных полигонов и ошибки
	processed := 0
	var processingError error

	// Обработка результатов по мере поступления для эффективного использования памяти
	for polygonResult := range results {
		// Централизованная обработка ошибок
		if polygonResult.err != nil {
			processingError = polygonResult.err
			log.Printf("Ошибка обработки многоугольника: %v", polygonResult.err)
			continue
		}

		// Безопасное обновление общего bbox - только в одной горутине
		result.Bbox.X1 = MinInt(result.Bbox.X1, polygonResult.localBbox.X1)
		result.Bbox.Y1 = MinInt(result.Bbox.Y1, polygonResult.localBbox.Y1)
		result.Bbox.X2 = MaxInt(result.Bbox.X2, polygonResult.localBbox.X2)
		result.Bbox.Y2 = MaxInt(result.Bbox.Y2, polygonResult.localBbox.Y2)

		// Безопасное обновление максимального веса
		result.MaxWeight = MaxFloat32(result.MaxWeight, polygonResult.weight)

		// Добавление тяжелых полигонов безопасно в одной горутине
		if polygonResult.isHeavy {
			result.HeavyPolygons = append(result.HeavyPolygons, polygonResult.polygon)
		}

		processed++
		
		// Отправка результата при обработке всех полигонов
		if processed == total {
			resCh <- result
			return
		}
	}

	// Обработка случаев неполного завершения
	// Улучшенная диагностика проблем с подробными сообщениями об ошибках
	if processed < total {
		if processingError != nil {
			log.Fatalf("Не все многоугольники обработаны: %v", processingError)
		} else if ctx.Err() != nil {
			log.Fatalf("Превышено время выполнения: %v", ctx.Err())
		} else {
			log.Fatalf("Неизвестная ошибка: обработано только %d из %d многоугольников", processed, total)
		}
	}
} 