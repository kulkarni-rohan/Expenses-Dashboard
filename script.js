document.addEventListener("DOMContentLoaded", function () {
  const tooltip = d3.select("#tooltip");
  const pieChart = d3.select("#pieChart");
  const barChart = d3.select("#barChart");

  let selectedCategories = new Set();
  let fullData = [];
  
  function loadData() {
    d3.csv("Expenses-Temporary-2.csv", d => ({
      date: new Date(d.date),
      category: d.category,
      amount: parseFloat(d.amount.replace(/[$,]/g, '')),
    })).then(data => {
      fullData = data;
      console.log("FULL DATA: ", fullData);
      const dates = data.map(d => d.date);
      const minDate = d3.min(dates);
      const maxDate = d3.max(dates);
  
      document.getElementById("fromDate").valueAsDate = minDate;
      document.getElementById("toDate").valueAsDate = maxDate;
  
      initializeCategoryFilter(data);
      drawDashboard(filterData(minDate, maxDate));
    });
  }
  
  function initializeCategoryFilter(data) {
    const categories = Array.from(new Set(data.map(d => d.category))).sort();
    const container = document.getElementById("categoryFilter");
    container.innerHTML = "";
  
    categories.forEach(cat => selectedCategories.add(cat));
  
    categories.forEach(cat => {
      const label = document.createElement("label");
      label.style.marginRight = "10px";
  
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.onchange = () => {
        if (checkbox.checked) {
          selectedCategories.add(cat);
        } else {
          selectedCategories.delete(cat);
        }
        applyGlobalFilters();
      };
  
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(" " + cat));
      container.appendChild(label);
    });
  }
  
  function applyGlobalFilters() {
    const from = new Date(document.getElementById("fromDate").value);
    const to = new Date(document.getElementById("toDate").value);
    drawDashboard(filterData(from, to));
  }
  
  function filterData(from, to) {
    return fullData.filter(d =>
      d.date >= from &&
      d.date <= to &&
      selectedCategories.has(d.category)
    );
  }
  
  document.getElementById("applyFilter").addEventListener("click", applyGlobalFilters);
  
  function drawDashboard(filteredData) {
    updateStats(filteredData);
    drawPieChart(filteredData);
    drawBarChart(filteredData);
    drawLineChart(filteredData);
    drawCalendarHeatmap(filteredData);
  }

  function updateStats(data) {
    const currentTotal = d3.sum(data, d => d.amount);
    const transactions = data.length;
  
    const dates = data.map(d => d.date).sort((a, b) => a - b);
    const minDate = dates[0];
    const maxDate = dates[dates.length - 1];
    const rangeDays = (maxDate - minDate) / (1000 * 60 * 60 * 24);
  
    const prevPeriod = fullData.filter(d => d.date >= new Date(minDate.getTime() - rangeDays * 24 * 60 * 60 * 1000) && d.date < minDate);
    const prevTotal = d3.sum(prevPeriod, d => d.amount);
    const prevTransactions = prevPeriod.length;
  
    const monthTotals = d3.rollup(data, v => d3.sum(v, d => d.amount), d => d3.timeFormat("%Y-%m")(d.date));
    const monthlyAvg = d3.mean([...monthTotals.values()]) || 0;
    const prevMonthTotals = d3.rollup(prevPeriod, v => d3.sum(v, d => d.amount), d => d3.timeFormat("%Y-%m")(d.date));
    const prevMonthlyAvg = d3.mean([...prevMonthTotals.values()]) || 0;

    console.log(monthlyAvg);
    console.log(prevMonthlyAvg);
  
    const byCategory = d3.rollup(data, v => d3.sum(v, d => d.amount), d => d.category);
    const top3 = [...byCategory.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
  
    document.getElementById("totalExpenses").textContent = `$${currentTotal.toFixed(2)}`;
    document.getElementById("avgMonthly").textContent = `$${monthlyAvg.toFixed(2)}`;
    document.getElementById("transactionCount").textContent = transactions;
  
    const topCategoriesDiv = document.getElementById("topCategories");
    topCategoriesDiv.innerHTML = "";
    const emojiMap = {
      "Rent": "🏠", "Grocery": "🛒", "Travel": "✈️",
      "Utilities": "💡", "Entertainment": "🎮", "Food": "🍽️", "Shopping": "🛍️", "Health": "💊", "Gas":"⛽", "Investment":"💵", "Charity":"✌️","Electronics":"💻","Family Support":"👨‍👩‍👧‍👦","House":"🏘️","Learning":"📚","Gift":"🎁","Networking":"🫂","Workshops":"🏢","Fees": "💳"
    };
  
    if (top3.length === 0) {
      topCategoriesDiv.innerHTML = "<div class='category-item'>None</div>";
    } else {
      top3.forEach(([category, amount]) => {
        const percent = ((amount / currentTotal) * 100).toFixed(1);
        const emoji = emojiMap[category] || "📁";
        const item = document.createElement("div");
        item.className = "category-item";
        item.innerHTML = `<span>${emoji} ${category}</span><span>$${amount.toFixed(2)} (${percent}%)</span>`;
        topCategoriesDiv.appendChild(item);
      });
    }
  
    function formatChange(curr, prev, positiveIsGood = false) {
      if (prev === 0) return { text: "0.0%", color: "green" };
      const change = ((curr - prev) / prev) * 100;
      const isPositive = change >= 0;
      const arrow = isPositive ? "↑" : "↓";
      const percent = `${arrow} ${Math.abs(change).toFixed(1)}%`;
      const color = (positiveIsGood ? isPositive : !isPositive) ? "green" : "red";
      return { text: `${percent} vs last period`, color };
    }
  
    const tChange = formatChange(currentTotal, prevTotal, false);
    const aChange = formatChange(monthlyAvg, prevMonthlyAvg, true);
    const txChange = formatChange(transactions, prevTransactions, false);
  
    document.getElementById("totalExpensesChange").textContent = tChange.text;
    document.getElementById("avgMonthlyChange").textContent = aChange.text;
    document.getElementById("transactionChange").textContent = txChange.text;
    document.getElementById("totalExpensesChange").className = `change ${tChange.color}`;
    document.getElementById("avgMonthlyChange").className = `change ${aChange.color}`;
    document.getElementById("transactionChange").className = `change ${txChange.color}`;
  }

  function drawPieChart(data) {
    const svg = d3.select("#pieChart");
    svg.selectAll("*").remove();
  
    const width = +svg.attr("width");
    const height = +svg.attr("height");
    const radius = Math.min(width, height) / 2;
  
    const group = svg.append("g")
      .attr("transform", `translate(${width / 2}, ${height / 2})`);
  
    const categoryTotals = d3.rollup(
      data,
      v => d3.sum(v, d => d.amount),
      d => d.category
    );
  
    const entries = [...categoryTotals.entries()];
    if (entries.length === 0) return;
  
    const total = d3.sum(entries, d => d[1]);
  
    const pie = d3.pie()
      .value(d => d[1])
      .sort(null);
  
    const arc = d3.arc()
      .innerRadius(60)
      .outerRadius(radius - 10);
  
    const color = d3.scaleOrdinal()
      .domain(entries.map(d => d[0]))
      .range(d3.schemeSet2);
  
    const tooltip = d3.select("#tooltip");
  
    group.selectAll("path")
      .data(pie(entries))
      .join("path")
      .attr("d", arc)
      .attr("fill", d => color(d.data[0]))
      .on("mouseover", (event, d) => {
        tooltip
          .style("opacity", 1)
          .html(`<strong>${d.data[0]}</strong><br>$${d.data[1].toFixed(2)} (${((d.data[1] / total) * 100).toFixed(1)}%)`);
      })
      .on("mousemove", event => {
        tooltip
          .style("left", (event.pageX + 15) + "px")
          .style("top", (event.pageY - 30) + "px");
      })
      .on("mouseout", () => tooltip.style("opacity", 0));
  
    group.selectAll("text")
      .data(pie(entries))
      .join("text")
      .attr("transform", d => `translate(${arc.centroid(d)})`)
      .attr("text-anchor", "middle")
      .attr("dy", "0.35em")
      .style("font-size", "12px")
      .style("font-weight", "500")
      .style("fill", "#1f2937")
      .text(d => `${d.data[0]} (${((d.data[1] / total) * 100).toFixed(1)}%)`);
  }

function drawBarChart(filteredData) {
  const svg = d3.select("#barChart");
  const width = +svg.attr("width");
  const height = +svg.attr("height");
  const margin = { top: 40, right: 30, bottom: 60, left: 60 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  svg.selectAll("*").remove();
  const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
  if (filteredData.length === 0) return;

  const formatter = d3.timeFormat("%Y-%m");
  const uniqueMonths = new Set(filteredData.map(d => formatter(d.date)));
  const isSingleMonth = uniqueMonths.size === 1;
  const groupBy = isSingleMonth
    ? d3.timeFormat("%d %b")
    : d3.timeFormat("%b %Y");

  // Group data: { label: { category: amount } }
  const grouped = d3.rollups(
    filteredData,
    v => {
      const totals = {};
      v.forEach(d => {
        if (!totals[d.category]) totals[d.category] = 0;
        totals[d.category] += d.amount;
      });
      return totals;
    },
    d => groupBy(d.date)
  );

  const labels = grouped.map(d => d[0]);
  const categories = Array.from(new Set(filteredData.map(d => d.category)));

  // Init selectedCategories on first load
  if (selectedCategories.size === 0) {
    categories.forEach(c => selectedCategories.add(c));
  }

  const filteredCategories = categories.filter(c => selectedCategories.has(c));

  const color = d3.scaleOrdinal()
    .domain(categories)
    .range(d3.schemeSet2);

  const filteredColor = d3.scaleOrdinal()
    .domain(filteredCategories)
    .range(d3.schemeSet2);

  // Build stacked data
  const stackedData = grouped.map(([label, catTotals]) => {
    const row = { label };
    categories.forEach(c => {
      row[c] = catTotals[c] || 0;
    });
    return row;
  });

  const stack = d3.stack().keys(filteredCategories);
  const series = stack(stackedData);

  const x = d3.scaleBand()
    .domain(labels)
    .range([0, innerWidth])
    .padding(0.2);

  const y = d3.scaleLinear()
    .domain([0, d3.max(stackedData, d => d3.sum(filteredCategories, c => d[c])) || 1])
    .nice()
    .range([innerHeight, 0]);

  // Y Axis
  g.append("g")
    .call(d3.axisLeft(y))
    .selectAll("text")
    .style("font-size", "12px")
    .style("fill", "#374151");

  g.append("text")
    .attr("text-anchor", "middle")
    .attr("transform", `rotate(-90)`)
    .attr("x", -innerHeight / 2)
    .attr("y", -45)
    .attr("fill", "#374151")
    .style("font-size", "13px")
    .style("font-weight", "bold")
    .text("Expenses ($)");

  // X Axis
  g.append("g")
    .attr("transform", `translate(0,${innerHeight})`)
    .call(d3.axisBottom(x))
    .selectAll("text")
    .attr("transform", "rotate(-30)")
    .style("text-anchor", "end")
    .style("font-size", "12px")
    .style("fill", "#374151");

  g.append("text")
    .attr("text-anchor", "middle")
    .attr("x", innerWidth / 2)
    .attr("y", innerHeight + 45)
    .attr("fill", "#374151")
    .style("font-size", "13px")
    .style("font-weight", "bold")
    .text(isSingleMonth ? "Day" : "Month");

  // Tooltip
  const tooltip = d3.select("#tooltip");

  // Stacked Bars
  g.selectAll("g.layer")
    .data(series)
    .join("g")
    .attr("fill", d => filteredColor(d.key))
    .attr("class", "layer")
    .selectAll("rect")
    .data(d => d.map(p => ({ ...p, category: d.key })))
    .join("rect")
    .attr("x", d => x(d.data.label))
    .attr("y", d => y(d[1]))
    .attr("height", d => y(d[0]) - y(d[1]))
    .attr("width", x.bandwidth())
    .on("mouseover", (event, d) => {
      tooltip
        .style("opacity", 1)
        .html(`<strong>${d.category}</strong><br>$${(d[1] - d[0]).toFixed(2)}`);
    })
    .on("mousemove", event => {
      tooltip
        .style("left", (event.pageX + 15) + "px")
        .style("top", (event.pageY - 30) + "px");
    })
    .on("mouseout", () => tooltip.style("opacity", 0));

  // ✅ LEGEND
  const legendContainer = document.getElementById("barLegend");
  legendContainer.innerHTML = ""; // Clear previous

  categories.forEach(cat => {
    const item = document.createElement("div");
    item.className = "legend-item";

    const box = document.createElement("span");
    box.className = "legend-color";
    box.style.backgroundColor = color(cat);
    box.style.opacity = selectedCategories.has(cat) ? "1" : "0.3";

    const label = document.createElement("span");
    label.textContent = cat;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedCategories.has(cat);
    checkbox.onchange = () => {
      if (checkbox.checked) {
        selectedCategories.add(cat);
      } else {
        selectedCategories.delete(cat);
      }
      drawBarChart(filteredData); // Redraw with updated categories
    };

    item.appendChild(checkbox);
    item.appendChild(box);
    item.appendChild(label);
    legendContainer.appendChild(item);
  });
}

function drawCalendarHeatmap(data) {
  const svg = d3.select("#calendarHeatmap");
  svg.selectAll("*").remove();

  const width = +svg.attr("width");
  const height = +svg.attr("height");
  const cellSize = 17;

  const dateFormat = d3.timeFormat("%Y-%m-%d");
  const color = d3.scaleSequential(d3.interpolateYlGnBu)
    .domain([0, d3.max(data, d => d.amount) || 1]);

  const byDay = d3.rollup(
    data,
    v => d3.sum(v, d => d.amount),
    d => dateFormat(d.date)
  );

  const groupedByYear = d3.groups(data, d => d.date.getFullYear());
  const years = groupedByYear.map(g => g[0]).sort();

  const g = svg.append("g").attr("transform", `translate(40, 20)`);

  years.forEach((year, yearIdx) => {
    const yearGroup = g.append("g").attr("transform", `translate(0, ${yearIdx * (cellSize * 10)})`);

    yearGroup.append("text")
      .attr("x", -10)
      .attr("y", -10)
      .attr("text-anchor", "start")
      .style("font-size", "14px")
      .style("font-weight", "600")
      .style("fill", "#1f2937")
      .text(year);

    const dates = d3.timeDays(new Date(year, 0, 1), new Date(year + 1, 0, 1));

    const tooltip = d3.select("#tooltip");

    yearGroup.selectAll("rect")
      .data(dates)
      .join("rect")
      .attr("width", cellSize)
      .attr("height", cellSize)
      .attr("rx", 3)
      .attr("ry", 3)
      .attr("x", d => d3.timeWeek.count(d3.timeYear(d), d) * cellSize)
      .attr("y", d => d.getDay() * cellSize)
      .attr("fill", d => {
        const key = dateFormat(d);
        return byDay.has(key) ? color(byDay.get(key)) : "#e5e7eb";
      })
      .style("stroke", "#fff")
      .style("stroke-width", "1px")
      .on("mouseover", (event, d) => {
        const key = dateFormat(d);
        const val = byDay.get(key);
        tooltip
          .style("opacity", 1)
          .html(`<strong>${key}</strong><br>$${val ? val.toFixed(2) : 0}`)
          .style("left", (event.pageX + 15) + "px")
          .style("top", (event.pageY - 30) + "px");
      })
      .on("mouseout", () => tooltip.style("opacity", 0));
  });
}


function drawLineChart(data) {
  const svg = d3.select("#lineChart");
  svg.selectAll("*").remove();

  const width = +svg.attr("width");
  const height = +svg.attr("height");
  const margin = { top: 30, right: 30, bottom: 60, left: 60 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;

  if (!data || data.length === 0) return;

  const monthlyTotals = d3.rollups(
    data,
    v => d3.sum(v, d => d.amount),
    d => d3.timeMonth(d.date)
  ).sort((a, b) => a[0] - b[0]);

  const x = d3.scaleTime()
    .domain(d3.extent(monthlyTotals, d => d[0]))
    .range([0, innerWidth]);

  const y = d3.scaleLinear()
    .domain([0, d3.max(monthlyTotals, d => d[1]) || 1])
    .nice()
    .range([innerHeight, 0]);

  const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);

  // 🧭 Y Axis
  g.append("g")
    .call(d3.axisLeft(y).ticks(5))
    .selectAll("text")
    .style("font-size", "12px")
    .style("fill", "#374151");

  g.append("text")
    .attr("text-anchor", "middle")
    .attr("transform", "rotate(-90)")
    .attr("x", -innerHeight / 2)
    .attr("y", -45)
    .style("fill", "#374151")
    .style("font-size", "13px")
    .style("font-weight", "bold")
    .text("Expenses ($)");

  // 🧭 X Axis
  g.append("g")
    .attr("transform", `translate(0,${innerHeight})`)
    .call(
      d3.axisBottom(x)
        .tickValues(monthlyTotals.map(d => d[0])) // only show labels for existing months
        .tickFormat(d3.timeFormat("%b %Y"))
    )    
    .selectAll("text")
    .attr("transform", "rotate(-30)")
    .style("text-anchor", "end")
    .style("font-size", "12px")
    .style("fill", "#374151");

  g.append("text")
    .attr("text-anchor", "middle")
    .attr("x", innerWidth / 2)
    .attr("y", innerHeight + 45)
    .style("fill", "#374151")
    .style("font-size", "13px")
    .style("font-weight", "bold")
    .text("Month");

  // 📈 Line path
  const line = d3.line()
    .x(d => x(d[0]))
    .y(d => y(d[1]));

  g.append("path")
    .datum(monthlyTotals)
    .attr("fill", "none")
    .attr("stroke", "#1f77b4")
    .attr("stroke-width", 2.5)
    .attr("d", line);

  // ⚪ Points
  g.selectAll("circle")
    .data(monthlyTotals)
    .join("circle")
    .attr("cx", d => x(d[0]))
    .attr("cy", d => y(d[1]))
    .attr("r", 4)
    .attr("fill", "#1f77b4");

  // 💬 Tooltip
  const tooltip = d3.select("#tooltip");

  g.selectAll("circle")
    .on("mouseover", (event, d) => {
      tooltip
        .style("opacity", 1)
        .html(`<strong>${d3.timeFormat("%B %Y")(d[0])}</strong><br>$${d[1].toFixed(2)}`);
    })
    .on("mousemove", (event) => {
      tooltip
        .style("left", (event.pageX + 15) + "px")
        .style("top", (event.pageY - 30) + "px");
    })
    .on("mouseout", () => tooltip.style("opacity", 0));
}



// Initialize
loadData();
  
});
