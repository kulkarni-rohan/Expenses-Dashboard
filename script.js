document.addEventListener("DOMContentLoaded", function () {
  const tooltip = d3.select("#tooltip");
  const pieChart = d3.select("#pieChart");
  const barChart = d3.select("#barChart");

  let fullData = [];

  d3.csv("expenses.csv", d => ({
    date: new Date(d.date),
    category: d.category,
    amount: +d.amount
  })).then(data => {
    fullData = data;

    // Set default date range
    const dates = data.map(d => d.date);
    const minDate = d3.min(dates);
    const maxDate = d3.max(dates);

    document.getElementById("fromDate").valueAsDate = minDate;
    document.getElementById("toDate").valueAsDate = maxDate;

    drawCharts(filterData(minDate, maxDate));
  });

  document.getElementById("applyFilter").addEventListener("click", () => {
    const from = new Date(document.getElementById("fromDate").value);
    const to = new Date(document.getElementById("toDate").value);
    drawCharts(filterData(from, to));
  });

  function filterData(from, to) {
    return fullData.filter(d => d.date >= from && d.date <= to);
  }

  function drawCharts(filteredData) {
    pieChart.selectAll("*").remove();
    barChart.selectAll("*").remove();

    updateStats(filteredData);

    const byCategory = d3.rollup(filteredData, v => d3.sum(v, d => d.amount), d => d.category);
    drawPieChart(byCategory);

    const byMonth = d3.rollup(filteredData, v => d3.sum(v, d => d.amount), d => d3.timeFormat("%b %Y")(d.date));
    drawBarChart(filteredData);
  }

  function updateStats(currentData) {
    console.log(currentData);
    const currentTotal = d3.sum(currentData, d => d.amount);
    const transactions = currentData.length;
  
    // Get min/max dates and filter last period
    const dates = currentData.map(d => d.date).sort((a, b) => a < b);
    console.log("Dates: ", dates);
    const minDate = dates[0];
    const maxDate = dates[dates.length - 1];
    const rangeDays = (maxDate - minDate) / (1000 * 60 * 60 * 24);
    const midDate = new Date(minDate.getTime() + (rangeDays / 2) * 24 * 60 * 60 * 1000);
  
    const prevPeriod = fullData.filter(d => d.date >= new Date(minDate.getTime() - rangeDays * 24 * 60 * 60 * 1000)
                                          && d.date < minDate);
    const prevTotal = d3.sum(prevPeriod, d => d.amount);
    const prevTransactions = prevPeriod.length;
  
    // Group current data by YYYY-MM and calculate monthly average
const monthTotals = d3.rollup(
  currentData,
  v => d3.sum(v, d => d.amount),
  d => d3.timeFormat("%Y-%m")(d.date)
);
const monthlyAvg = d3.mean([...monthTotals.values()]) || 0;

// Group previous data by YYYY-MM and calculate previous monthly average
const prevMonthTotals = d3.rollup(
  prevPeriod,
  v => d3.sum(v, d => d.amount),
  d => d3.timeFormat("%Y-%m")(d.date)
);
const prevMonthlyAvg = d3.mean([...prevMonthTotals.values()]) || 0;

  
    const byCategory = d3.rollup(currentData, v => d3.sum(v, d => d.amount), d => d.category);
    const top = [...byCategory.entries()].sort((a, b) => b[1] - a[1])[0];
  
    // Update DOM
    document.getElementById("totalExpenses").textContent = `$${currentTotal.toFixed(2)}`;
    document.getElementById("avgMonthly").textContent = `$${monthlyAvg.toFixed(2)}`;
    // Get top 3 categories
const top3 = [...byCategory.entries()]
.sort((a, b) => b[1] - a[1])
.slice(0, 3);

const topCategoriesDiv = document.getElementById("topCategories");
topCategoriesDiv.innerHTML = ""; // Clear previous

if (top3.length === 0) {
topCategoriesDiv.innerHTML = "<div class='category-item'>None</div>";
} else {
  const emojiMap = {
    "Rent": "🏠",
    "Groceries": "🛒",
    "Transportation": "🚌",
    "Utilities": "💡",
    "Entertainment": "🎮",
    "Dining": "🍽️",
    "Travel": "✈️",
    "Shopping": "🛍️",
    "Healthcare": "💊",
    "Other": "🔹"
  };
  
  top3.forEach(([category, amount]) => {
    const percent = ((amount / currentTotal) * 100).toFixed(1);
    const emoji = emojiMap[category] || "📁";
  
    const item = document.createElement("div");
    item.className = "category-item";
    item.innerHTML = `<span>${emoji} ${category}</span><span>$${amount.toFixed(2)} (${percent}%)</span>`;
    topCategoriesDiv.appendChild(item);
  });
  
}

    document.getElementById("transactionCount").textContent = transactions;
  
    // Change helpers
    function formatChange(curr, prev, positiveIsGood = false) {
      if (prev === 0) {
        return { text: "0.0%", color: "green" }; // Neutral by default
      }
      const change = ((curr - prev) / prev) * 100;
      const isPositive = change >= 0;
    
      const arrow = isPositive ? "↑" : "↓";
      const percent = `${arrow} ${Math.abs(change).toFixed(1)}%`;
      const color = (positiveIsGood ? isPositive : !isPositive) ? "green" : "red";
    
      return {
        text: `${percent} vs last period`,
        color
      };
    }

    const tChange = formatChange(currentTotal, prevTotal, false);   // more is bad
    const aChange = formatChange(monthlyAvg, prevMonthlyAvg, false); // more is good
    const txChange = formatChange(transactions, prevTransactions, false); // more is bad

  
    document.getElementById("totalExpensesChange").textContent = tChange.text;
    document.getElementById("avgMonthlyChange").textContent = aChange.text;
    document.getElementById("transactionChange").textContent = txChange.text;
  
    document.getElementById("totalExpensesChange").className = `change ${tChange.color}`;
    document.getElementById("avgMonthlyChange").className = `change ${aChange.color}`;
    document.getElementById("transactionChange").className = `change ${txChange.color}`;
  }

  function drawPieChart(dataMap) {
    const svg = d3.select("#pieChart");
    const width = +svg.attr("width");
    const height = +svg.attr("height");
    const radius = Math.min(width, height) / 2;
    const group = svg.append("g").attr("transform", `translate(${width / 2}, ${height / 2})`);

    const pie = d3.pie().value(d => d[1]);
    const arc = d3.arc().innerRadius(60).outerRadius(radius - 10);
    const color = d3.scaleOrdinal(d3.schemeSet2);

    group.selectAll("path")
      .data(pie([...dataMap]))
      .enter()
      .append("path")
      .attr("fill", d => color(d.data[0]))
      .attr("d", arc)
      .on("mouseover", (event, d) => {
        tooltip
          .style("opacity", 1)
          .html(`<strong>${d.data[0]}</strong><br>$${d.data[1]}`);
      })
      .on("mousemove", (event) => {
        tooltip
          .style("left", (event.pageX + 15) + "px")
          .style("top", (event.pageY - 30) + "px");
      })
      .on("mouseout", () => tooltip.style("opacity", 0));

      group.selectAll("text")
      .data(pie([...dataMap]))
      .enter()
      .append("text")
      .attr("transform", d => `translate(${arc.centroid(d)})`)
      .attr("text-anchor", "middle")
      .attr("dy", "0.35em")
      .style("font-size", "12px")
      .style("font-weight", "500")
      .style("fill", "#1f2937")
      .text(d => `${d.data[0]} (${((d.data[1] / d3.sum(dataMap.values())) * 100).toFixed(1)}%)`);
    
  }

  let selectedCategories = new Set(); // Persistent set to track visible categories

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

  
});
